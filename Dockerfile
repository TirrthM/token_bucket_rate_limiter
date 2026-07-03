# Start from an official small Python image
FROM python:3.12-slim

# All following commands run inside /app in the container
WORKDIR /app

# Copy ONLY requirements first, then install.
# Docker caches each step — as long as requirements.txt is unchanged,
# rebuilds skip the slow pip install. Copying all code first would
# bust the cache on every code edit. Classic optimization.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Now copy the rest of the project
COPY . .

# Start the server. --host 0.0.0.0 = accept connections from outside
# the container (127.0.0.1 would only accept from inside it — common trap).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]