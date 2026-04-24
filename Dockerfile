FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY px2tg_main.py .
COPY Pixar2Tele/ Pixar2Tele/
COPY pixiv404.png .

CMD ["python", "-u", "px2tg_main.py"]
