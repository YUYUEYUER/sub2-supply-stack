FROM python:3.12.10-alpine3.21

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN addgroup -S -g 10001 bridge && adduser -S -D -H -u 10001 -G bridge bridge
COPY --chown=bridge:bridge bridge /app/bridge
RUN python -m compileall -q /app/bridge

USER bridge
EXPOSE 8080 8081
CMD ["python", "-m", "bridge"]
