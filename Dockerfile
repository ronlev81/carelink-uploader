FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY uploader.py carelink_client.py .
CMD ["python", "-u", "uploader.py"]
