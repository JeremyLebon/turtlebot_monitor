FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    nmap \
    avahi-utils \
    libnss-mdns \
    iproute2 \
    net-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY turtlebots.json .

EXPOSE 5000
CMD ["python", "app.py"]
