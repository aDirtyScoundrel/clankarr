FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY clankarr.py .
EXPOSE 8099
CMD ["python", "clankarr.py"]
