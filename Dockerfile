FROM python:3.12-slim

# Minimal system deps for pdfplumber/Pillow
RUN apt-get update && apt-get install -y \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install lightweight deploy deps (no GUI/OCR/Windows)
COPY requirements-deploy.txt .
RUN pip install --no-cache-dir -r requirements-deploy.txt

# Copy project
COPY . .

# Create directories
RUN mkdir -p logs/screenshots storage/checkpoints storage/checkpoints reports

# Run Telegram bot
CMD ["python", "telegram_bot/bot.py"]
