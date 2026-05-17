FROM python:3.12-slim

# System deps for opencv, easyocr, python-docx
RUN apt-get update && apt-get install -y \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    libgl1-mesa-glx \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . .

# Create directories
RUN mkdir -p logs/screenshots storage/checkpoints storage/checkpoints reports

# Run Telegram bot
CMD ["python", "telegram_bot/bot.py"]
