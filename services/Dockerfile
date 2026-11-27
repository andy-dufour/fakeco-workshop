# One image for every Python service, the seed job and the load generator. The command picks which one runs:
#   python -m fakeco.run <service>     a service of fakeco/config.py
#   python -m fakeco.seed              fill the database once
#   python -m fakeco.loadgen           the load generator
FROM python:3.15-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY fakeco ./fakeco
USER 65534
EXPOSE 8080
ENTRYPOINT ["python", "-m"]
CMD ["fakeco.run", "concierge-api"]
