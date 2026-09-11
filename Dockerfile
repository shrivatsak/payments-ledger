FROM python:3.13-slim

WORKDIR /app

# Dependencies first, in their own layer, so ordinary code changes don't reinstall
# them. pip has no "dependencies only" mode for a pyproject, so the list is read
# out of the [project] table with the standard-library tomllib. The project itself
# is not installed; the code runs from the working directory as before.
COPY pyproject.toml .
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" > /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
