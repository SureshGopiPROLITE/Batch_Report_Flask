# Base Image
FROM python:3.12-slim-bookworm

# Working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    tzdata \
    python3-tk \
    tk \
    tcl \
    tk-dev \
    wget \
    fontconfig \
    pkg-config \
    libcairo2 \
    libcairo2-dev \
    libgirepository1.0-dev \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libglib2.0-0 \
    libglib2.0-dev \
    libffi8 \
    libffi-dev \
    libharfbuzz0b \
    libfreetype6 \
    libjpeg62-turbo \
    libjpeg62-turbo-dev \
    libpng16-16 \
    libx11-6 \
    libxext6 \
    libxrender1 \
    libxft2 \
    build-essential \
    xz-utils \
    && ln -sf /usr/share/zoneinfo/Asia/Kolkata /etc/localtime \
    && echo "Asia/Kolkata" > /etc/timezone \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Kolkata
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .

RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000
EXPOSE 8050

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:5000", "--workers", "1", "--timeout", "120"]