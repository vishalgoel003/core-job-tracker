FROM python:3.11-slim

WORKDIR /app

# Install system dependencies if required by python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy static application code
COPY src/ ./src/
COPY web/ ./web/
COPY deploy/start.sh ./deploy/start.sh

# Make start script executable
RUN chmod +x deploy/start.sh

# Streamlit exposes port 8501 by default
EXPOSE 8501

ENTRYPOINT ["./deploy/start.sh"]
