FROM python:3.11-slim

# System dependencies for Chrome, Playwright, Node.js, and PDF generation
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg2 ca-certificates \
    libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    libxshmfence1 libx11-xcb1 libxcomposite1 libxdamage1 \
    libxrandr2 libxfixes3 libcups2 libdbus-1-3 \
    fonts-liberation \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Google Chrome stable
RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor > /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 20 LTS (needed for npx to run Playwright MCP server)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

ENV CHROME_PATH=/usr/bin/google-chrome-stable

WORKDIR /app

# Install Python dependencies (README.md required by pyproject.toml for metadata)
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir -e .

# Install python-jobspy separately (broken numpy pin)
RUN pip install --no-cache-dir --no-deps python-jobspy \
    && pip install --no-cache-dir pydantic tls-client requests markdownify regex

# S3 upload for Runforge artifact reporting
RUN pip install --no-cache-dir boto3

# Install Runforge SDK (agent-runtime)
RUN pip install --no-cache-dir agent-runtime

# Install Playwright browsers (for enrichment that might use playwright)
RUN pip install playwright && playwright install chromium && playwright install-deps chromium

# Copy source code
COPY . .

RUN mkdir -p /root/.applypilot
ENV APPLYPILOT_DIR=/root/.applypilot
ENV AGENT_RUNTIME_ENV=production

# Runforge SDK runs the agent (entrypoint: agent:run_applypilot)
CMD ["python", "-m", "agent_runtime", "worker", "agent:run_applypilot"]
