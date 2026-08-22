FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml ./
COPY README.md ./

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

COPY fantasy_gm ./fantasy_gm

RUN pip install --no-cache-dir .

EXPOSE 8000

CMD ["uvicorn", "fantasy_gm.web.app:app", "--host", "0.0.0.0", "--port", "8000"]