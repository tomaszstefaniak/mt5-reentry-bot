# Dockerfile
FROM python:3.10-slim

WORKDIR /app

# Copy your code
COPY . .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Use $PORT that Vercel provides
ENV PORT=${PORT:-5000}

# Expose and launch
EXPOSE $PORT
CMD ["sh", "-c", "python mt5.py"]
