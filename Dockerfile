FROM python:3.12-slim

# nodejs (fallback) + deno (yt-dlp's preferred JS runtime for YouTube challenges)
RUN apt-get update \
 && apt-get install -y --no-install-recommends nodejs curl unzip \
 && curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .

CMD ["python3", "bot.py"]
