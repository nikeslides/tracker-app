FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements file
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Ensure entrypoint is executable (redundant but safe)
RUN chmod +x entrypoint.sh

# Expose port 5000
EXPOSE 5000

# Default environment variables
ENV OUTPUT_PATH=/app/data
ENV SHEET_CSV_PATH=/app/data/sheet.csv
ENV APP_USERNAME=admin
ENV APP_PASSWORD=password

# Volume for persistent data (audio, artwork, sheet data)
VOLUME ["/app/data"]

# Use entrypoint script
ENTRYPOINT ["./entrypoint.sh"]

