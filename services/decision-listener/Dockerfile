# [LOCAL-FORK] Decision Listener — real-time meeting analysis using local Ollama LLM
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 8766

CMD ["python", "main.py"]
