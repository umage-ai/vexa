// [LOCAL-FORK] Vision snapshot service — periodically screenshots the meeting,
// uploads to bot-manager, describes via Ollama vision model, and publishes
// the description as a "[Vision]" transcription segment to Redis.

import { Page } from 'playwright-core';
import { BotConfig } from '../types';
import { log } from '../utils';
import http from 'http';
import https from 'https';
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

    // Upload screenshot to bot-manager (fire-and-forget)
    this._uploadScreenshot(screenshotBuffer).catch((err) => {
      log(`[Vision] Upload error (non-fatal): ${err.message}`);
    });

    // Describe via Ollama vision model (fire-and-forget with publish on success)
    if (this.botConfig.visionModelUrl && this.botConfig.visionModelName) {
      this._describeImage(screenshotBuffer).catch((err) => {
        log(`[Vision] Describe error (non-fatal): ${err.message}`);
      });
    }
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

  private async _describeImage(imageBuffer: Buffer): Promise<void> {
    const modelUrl = this.botConfig.visionModelUrl!;
    const modelName = this.botConfig.visionModelName!;
    const base64Image = imageBuffer.toString('base64');

    const requestBody = JSON.stringify({
      model: modelName,
      messages: [{
        role: 'user',
        content: 'Describe what is being shown on this screen share during a meeting. Be concise, focus on the main content visible.',
        images: [base64Image],
      }],
      stream: false,
    });

    const description = await new Promise<string>((resolve, reject) => {
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

    if (description && !this.stopped) {
      log(`[Vision] Description: "${description.substring(0, 120)}..."`);
      await this._publishVisionSegment(description);
    }
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
