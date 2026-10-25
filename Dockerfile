
FROM node:20-bookworm-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY package.json ./
RUN npm install --omit=dev

COPY requirements.txt ./
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

ENV PATH="/opt/venv/bin:$PATH"

COPY . .

RUN printf '#!/bin/sh\nset -e\npython /app/ai_service.py &\nexec node /app/baileys.js\n' > /app/start.sh \
    && chmod +x /app/start.sh

EXPOSE 10000

CMD ["/app/start.sh"]
