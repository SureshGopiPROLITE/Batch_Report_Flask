FROM python:3.12-slim-bookworm

WORKDIR /app

# Install system libraries required for WeasyPrint and timezone
RUN apt-get update && apt-get install -y \
    tzdata \
    python3-tk \
    tk \
    tcl \
    tk-dev \
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
    && ln -sf /usr/share/zoneinfo/Asia/Kolkata /etc/localtime \
    && echo "Asia/Kolkata" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# Set container timezone to IST
ENV TZ=Asia/Kolkata

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Expose Flask and Dash ports
EXPOSE 5000
EXPOSE 8050

# Run with single worker
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:5000", "--timeout", "120"]