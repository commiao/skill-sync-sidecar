FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

RUN useradd --create-home --shell /usr/sbin/nologin skill-sync \
    && mkdir -p /cache \
    && chown -R skill-sync:skill-sync /cache
USER skill-sync

EXPOSE 8765

ENTRYPOINT ["skill-sync"]
CMD ["gateway", "--help"]
