FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PORT=8000
ENV PROJECT_ROOT=/app
ENV DATA_ROOT=/data

# Install system dependencies, including ffmpeg, libgl1 for OpenCV, build-essential for lap
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    build-essential \
    nodejs \
    npm \
    curl \
    unzip \
    git \
    wget \
    fonts-noto \
    fonts-noto-cjk \
    fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*


# Install Deno and bgutil-ytdlp-pot-provider for YouTube PO Tokens
ENV DENO_INSTALL=/root/.deno
ENV PATH=${DENO_INSTALL}/bin:${PATH}
RUN curl -fsSL https://deno.land/install.sh | sh \
    && git clone --single-branch --branch 2.0.0 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil-ytdlp-pot-provider \
    && cd /opt/bgutil-ytdlp-pot-provider/server/ \
    && deno install --allow-scripts=npm:canvas --frozen


WORKDIR /app

# Upgrade pip and install wheel
RUN pip install --upgrade pip wheel

# Install Python dependencies
COPY requirements_linux.txt .
RUN pip install --no-cache-dir -r requirements_linux.txt

# Copy application source
COPY app/ ./app/
COPY scripts/ ./scripts/

EXPOSE 8000

# Start command
ENV PYTHONPATH=/app
CMD ["python", "-m", "app.main"]
