FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer-cached until requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY app.py config.py settings.py auth.py kavita.py metadata.py ./
COPY routes/ ./routes/
COPY templates/ ./templates/

# Data directory — mount a volume here to persist settings and logs
RUN mkdir -p /data
ENV DATA_DIR=/data

# Optional: override the listening port (default 8080)
ENV PORT=8080
EXPOSE ${PORT}

CMD ["python", "app.py"]
