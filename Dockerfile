FROM python:3.12-slim-bookworm

WORKDIR /app

# Install system libraries required for WeasyPrint (ARM64 compatible)
RUN apt-get update && apt-get install -y \
    wget \
    fontconfig \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libglib2.0-0 \
    libffi8 \
    libharfbuzz0b \
    libfreetype6 \
    libjpeg62-turbo \
    libpng16-16 \
    libx11-6 \
    libxext6 \
    libxrender1 \
    libxft2 \
    build-essential \
    xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app files
COPY . .

# Expose ports for Flask & Dash
EXPOSE 5000
EXPOSE 8050

# Optimized Gunicorn command
CMD ["gunicorn", "app:app", "-w", "2", "-k", "gthread", "--threads", "2", "--bind", "0.0.0.0:5000"]


