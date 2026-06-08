FROM python:3.12-slim

WORKDIR /app

# Install uv for dependency management
RUN pip install --no-cache-dir uv

# Install dependencies from pyproject.toml
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen

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

CMD ["uv", "run", "python", "app.py"]
