// [LOCAL-FORK] Vision snapshot service — periodically screenshots the meeting,
// uploads to bot-manager, describes via Ollama vision model, and publishes
// the description as a "[Vision]" transcription segment to Redis.

import { Page } from 'playwright-core';
import { BotConfig } from '../types';
import { log } from '../utils';
import http from 'http';
import https from 'https';
import crypto from 'crypto'; // [LOCAL-FORK] for change detection hashing
import { createClient, RedisClientType } from 'redis';

export class VisionSnapshotService {
  private page: Page;
  private botConfig: BotConfig;
  private intervalHandle: ReturnType<typeof setInterval> | null = null;
  private redisPublisher: RedisClientType | null = null;
  private sessionStartTimeMs: number = Date.now();
  private sessionStartPublished: boolean = false;
  private visionSessionUid: string;
  private stopped: boolean = false;

  // [LOCAL-FORK] Change detection state
  private previousHash: string | null = null;
  private previousBuffer: Buffer | null = null;
  private static readonly CHANGE_THRESHOLD = 0.15; // 15% of sampled bytes must differ
  private static readonly SAMPLE_STEP = 1000; // sample every 1000th byte
  private static readonly BYTE_DIFF_THRESHOLD = 10; // byte values must differ by more than this

  constructor(page: Page, botConfig: BotConfig) {
    this.page = page;
    this.botConfig = botConfig;
    // Derive a unique session UID for vision segments (similar to chat service pattern)
    this.visionSessionUid = `vision-${botConfig.connectionId}`;
  }

  async start(): Promise<void> {
    const intervalMs = this.botConfig.visionSnapshotIntervalMs || 30000;
    log(`[Vision] Starting snapshot service (interval=${intervalMs}ms, model=${this.botConfig.visionModelName || 'n/a'})`);

    this.sessionStartTimeMs = Date.now();
    this.stopped = false;

    // Initialize Redis publisher for transcript injection
    if (this.botConfig.redisUrl) {
      try {
        this.redisPublisher = createClient({ url: this.botConfig.redisUrl }) as RedisClientType;
        this.redisPublisher.on('error', (err) => log(`[Vision] Redis error: ${err}`));
        await this.redisPublisher.connect();
        log('[Vision] Redis publisher connected');
      } catch (err: any) {
        log(`[Vision] Failed to connect Redis: ${err.message}`);
        this.redisPublisher = null;
      }
    }

    // Run first tick immediately, then on interval
    this._tick().catch((err) => log(`[Vision] Initial tick error: ${err.message}`));
    this.intervalHandle = setInterval(() => {
      this._tick().catch((err) => log(`[Vision] Tick error: ${err.message}`));
    }, intervalMs);
  }

  stop(): void {
    log('[Vision] Stopping snapshot service');
    this.stopped = true;
    if (this.intervalHandle) {
      clearInterval(this.intervalHandle);
      this.intervalHandle = null;
    }
    // Publish session_end and close Redis
    this._publishSessionEnd().catch(() => {}).finally(() => {
      if (this.redisPublisher) {
        this.redisPublisher.quit().catch(() => {});
        this.redisPublisher = null;
      }
    });
  }

  // ==================== Core tick ====================

  private async _tick(): Promise<void> {
    if (this.stopped) return;

    let screenshotBuffer: Buffer;
    try {
      screenshotBuffer = await this.page.screenshot({ type: 'jpeg', quality: 80 }) as Buffer;
      log(`[Vision] Screenshot captured (${screenshotBuffer.length} bytes)`);
    } catch (err: any) {
      log(`[Vision] Screenshot failed: ${err.message}`);
      return;
    }

    // [LOCAL-FORK] Change detection — skip if image hasn't changed substantially
    if (!this._hasSubstantialChange(screenshotBuffer)) {
      log('[Vision] No substantial change detected, skipping tick');
      return;
    }

    // [LOCAL-FORK] Update stored previous screenshot for next comparison
    this.previousHash = crypto.createHash('md5').update(screenshotBuffer).digest('hex');
    this.previousBuffer = screenshotBuffer;

    // [LOCAL-FORK] Content filtering — ask LLM if this is shared content vs webcam feeds.
    // If the vision model is configured, we use the combined prompt that both classifies
    // and describes. Upload and publish only happen if the LLM says it's shared content.
    if (this.botConfig.visionModelUrl && this.botConfig.visionModelName) {
      this._classifyAndDescribeImage(screenshotBuffer).catch((err) => {
        log(`[Vision] Classify+describe error (non-fatal): ${err.message}`);
      });
    } else {
      // No vision model configured — fall back to upload-only (original behavior)
      this._uploadScreenshot(screenshotBuffer).catch((err) => {
        log(`[Vision] Upload error (non-fatal): ${err.message}`);
      });
    }
  }

  // [LOCAL-FORK] Determine if the screenshot has changed substantially from the previous one.
  // Uses MD5 for exact-match check, then byte-sampling for fuzzy comparison.
  private _hasSubstantialChange(currentBuffer: Buffer): boolean {
    if (!this.previousBuffer || !this.previousHash) {
      // First screenshot — always process
      return true;
    }

    // Exact match via MD5
    const currentHash = crypto.createHash('md5').update(currentBuffer).digest('hex');
    if (currentHash === this.previousHash) {
      return false;
    }

    // Fuzzy comparison: sample every Nth byte and count substantial differences
    const prev = this.previousBuffer;
    const curr = currentBuffer;
    const minLen = Math.min(prev.length, curr.length);
    const step = VisionSnapshotService.SAMPLE_STEP;
    const diffThreshold = VisionSnapshotService.BYTE_DIFF_THRESHOLD;

    let sampledCount = 0;
    let diffCount = 0;

    for (let i = 0; i < minLen; i += step) {
      sampledCount++;
      if (Math.abs(prev[i] - curr[i]) > diffThreshold) {
        diffCount++;
      }
    }

    // Also account for significant size difference as a change signal
    const sizeDiffRatio = Math.abs(prev.length - curr.length) / Math.max(prev.length, curr.length);
    if (sizeDiffRatio > 0.1) {
      log(`[Vision] Size difference ${(sizeDiffRatio * 100).toFixed(1)}% — treating as substantial change`);
      return true;
    }

    const changeRatio = sampledCount > 0 ? diffCount / sampledCount : 0;
    log(`[Vision] Change detection: ${(changeRatio * 100).toFixed(1)}% of sampled bytes differ (threshold: ${VisionSnapshotService.CHANGE_THRESHOLD * 100}%)`);
    return changeRatio >= VisionSnapshotService.CHANGE_THRESHOLD;
  }

  // ==================== Screenshot upload ====================

  private async _uploadScreenshot(imageBuffer: Buffer): Promise<void> {
    const uploadUrl = this.botConfig.recordingUploadUrl;
    if (!uploadUrl) {
      log('[Vision] No recordingUploadUrl configured, skipping upload');
      return;
    }

    const boundary = `----VexaVisionSnapshot${Date.now()}`;
    const parts: Buffer[] = [];

    // session_uid field
    parts.push(Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="session_uid"\r\n\r\n`));
    parts.push(Buffer.from(this.botConfig.connectionId));
    parts.push(Buffer.from('\r\n'));

    // media_type field
    parts.push(Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="media_type"\r\n\r\n`));
    parts.push(Buffer.from('screenshot'));
    parts.push(Buffer.from('\r\n'));

    // media_format field
    parts.push(Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="media_format"\r\n\r\n`));
    parts.push(Buffer.from('jpg'));
    parts.push(Buffer.from('\r\n'));

    // metadata field
    parts.push(Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="metadata"\r\nContent-Type: application/json\r\n\r\n`));
    parts.push(Buffer.from('{}'));
    parts.push(Buffer.from('\r\n'));

    // is_final field
    parts.push(Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="is_final"\r\n\r\n`));
    parts.push(Buffer.from('false'));
    parts.push(Buffer.from('\r\n'));

    // file field
    parts.push(Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="screenshot.jpg"\r\nContent-Type: image/jpeg\r\n\r\n`));
    parts.push(imageBuffer);
    parts.push(Buffer.from(`\r\n--${boundary}--\r\n`));

    const body = Buffer.concat(parts);

    return new Promise((resolve, reject) => {
      const url = new URL(uploadUrl);
      const transport = url.protocol === 'https:' ? https : http;
      const req = transport.request(
        {
          hostname: url.hostname,
          port: url.port,
          path: url.pathname,
          method: 'POST',
          headers: {
            'Content-Type': `multipart/form-data; boundary=${boundary}`,
            'Content-Length': body.length,
            'Authorization': `Bearer ${this.botConfig.token}`,
          },
        },
        (res) => {
          let responseData = '';
          res.on('data', (chunk) => { responseData += chunk; });
          res.on('end', () => {
            if (res.statusCode && res.statusCode >= 200 && res.statusCode < 300) {
              log(`[Vision] Screenshot upload successful: ${res.statusCode}`);
              resolve();
            } else {
              reject(new Error(`Upload failed with status ${res.statusCode}: ${responseData}`));
            }
          });
        }
      );
      req.on('error', (err) => reject(err));
      req.write(body);
      req.end();
    });
  }

  // ==================== Ollama vision description ====================

  // [LOCAL-FORK] Combined classify + describe: asks the LLM whether this is shared content
  // (screen share, presentation, document, app) vs participant video feeds, and if so,
  // what is being shown. Only uploads and publishes when content is meaningful.
  private async _classifyAndDescribeImage(imageBuffer: Buffer): Promise<void> {
    const modelUrl = this.botConfig.visionModelUrl!;
    const modelName = this.botConfig.visionModelName!;
    const base64Image = imageBuffer.toString('base64');

    // [LOCAL-FORK] Combined prompt: classify AND describe in one call
    const prompt = [
      'Analyze this screenshot from a video meeting. Answer TWO questions:',
      '1. Is this a screen share, presentation, document, or application being shared? Or is it just showing participant video feeds / webcam gallery view?',
      '2. If it IS shared content (not just webcam feeds), describe what is being shown. Be concise, focus on the main content visible.',
      '',
      'Reply ONLY with JSON (no markdown fences): {"is_shared_content": true/false, "description": "..."}',
      'If is_shared_content is false, set description to an empty string.',
    ].join('\n');

    const requestBody = JSON.stringify({
      model: modelName,
      messages: [{
        role: 'user',
        content: prompt,
        images: [base64Image],
      }],
      stream: false,
    });

    const rawResponse = await new Promise<string>((resolve, reject) => {
      const url = new URL(`${modelUrl}/api/chat`);
      const transport = url.protocol === 'https:' ? https : http;
      const req = transport.request(
        {
          hostname: url.hostname,
          port: url.port,
          path: url.pathname,
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Content-Length': Buffer.byteLength(requestBody),
          },
        },
        (res) => {
          let responseData = '';
          res.on('data', (chunk) => { responseData += chunk; });
          res.on('end', () => {
            if (res.statusCode && res.statusCode >= 200 && res.statusCode < 300) {
              try {
                const parsed = JSON.parse(responseData);
                const text = parsed?.message?.content || parsed?.response || '';
                resolve(text);
              } catch {
                reject(new Error(`Failed to parse Ollama response: ${responseData.substring(0, 200)}`));
              }
            } else {
              reject(new Error(`Ollama request failed: ${res.statusCode} - ${responseData.substring(0, 200)}`));
            }
          });
        }
      );
      req.on('error', (err) => reject(err));
      req.write(requestBody);
      req.end();
    });

    if (!rawResponse || this.stopped) return;

    // [LOCAL-FORK] Parse the JSON classification response
    let classification: { is_shared_content: boolean; description: string };
    try {
      // Strip markdown fences if the model wrapped it anyway
      const cleaned = rawResponse.replace(/```json\s*/gi, '').replace(/```\s*/g, '').trim();
      classification = JSON.parse(cleaned);
    } catch {
      // If we can't parse JSON, fall back: treat as shared content with the raw text as description
      log(`[Vision] Could not parse classification JSON, falling back to raw description`);
      classification = { is_shared_content: true, description: rawResponse };
    }

    if (!classification.is_shared_content) {
      log('[Vision] LLM says this is participant video feeds, skipping upload+publish');
      return;
    }

    const description = classification.description || '';
    if (!description) {
      log('[Vision] LLM classified as shared content but description is empty, skipping');
      return;
    }

    log(`[Vision] Shared content detected: "${description.substring(0, 120)}..."`);

    // [LOCAL-FORK] Only upload and publish for meaningful shared content
    this._uploadScreenshot(imageBuffer).catch((err) => {
      log(`[Vision] Upload error (non-fatal): ${err.message}`);
    });

    await this._publishVisionSegment(description);
  }

  // ==================== Redis transcript publishing ====================

  private async _ensureSessionStart(): Promise<void> {
    if (this.sessionStartPublished || !this.redisPublisher) return;

    const streamKey = 'transcription_segments';
    const payload = JSON.stringify({
      type: 'session_start',
      token: this.botConfig.token,
      platform: this.botConfig.platform,
      meeting_id: this.botConfig.meeting_id,
      uid: this.visionSessionUid,
      start_timestamp: new Date().toISOString(),
    });

    try {
      await this.redisPublisher.xAdd(streamKey, '*', { payload });
      this.sessionStartPublished = true;
      log(`[Vision] Published session_start for UID: ${this.visionSessionUid}`);
    } catch (err: any) {
      log(`[Vision] Failed to publish session_start: ${err.message}`);
    }
  }

  private async _publishVisionSegment(description: string): Promise<void> {
    if (!this.redisPublisher) return;

    await this._ensureSessionStart();

    const relativeTimeSec = (Date.now() - this.sessionStartTimeMs) / 1000;
    const segmentDuration = 1.0; // 1 second nominal duration for vision segments

    const streamKey = 'transcription_segments';
    const payload = JSON.stringify({
      type: 'transcription',
      token: this.botConfig.token,
      platform: this.botConfig.platform,
      meeting_id: this.botConfig.meeting_id,
      uid: this.visionSessionUid,
      segments: [{
        start: relativeTimeSec,
        end: relativeTimeSec + segmentDuration,
        text: `[Vision] ${description}`,
        language: 'en',
        completed: true,
      }],
    });

    try {
      await this.redisPublisher.xAdd(streamKey, '*', { payload });
      log(`[Vision] Published vision segment (t=${relativeTimeSec.toFixed(1)}s)`);
    } catch (err: any) {
      log(`[Vision] Failed to publish vision segment: ${err.message}`);
    }
  }

  private async _publishSessionEnd(): Promise<void> {
    if (!this.sessionStartPublished || !this.redisPublisher) return;

    const streamKey = 'transcription_segments';
    const payload = JSON.stringify({
      type: 'session_end',
      token: this.botConfig.token,
      platform: this.botConfig.platform,
      meeting_id: this.botConfig.meeting_id,
      uid: this.visionSessionUid,
      end_timestamp: new Date().toISOString(),
    });

    try {
      await this.redisPublisher.xAdd(streamKey, '*', { payload });
      log(`[Vision] Published session_end for UID: ${this.visionSessionUid}`);
    } catch (err: any) {
      log(`[Vision] Failed to publish session_end: ${err.message}`);
    }
  }
}
