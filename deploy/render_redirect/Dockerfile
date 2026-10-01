# The retired Render address (smb-broker.onrender.com): one stdlib file that answers 308 to the live
# server. No dependencies, no secrets. Render sets PORT.
FROM python:3.12-alpine
WORKDIR /app
COPY redirect_server.py .
ENV PYTHONUNBUFFERED=1
USER nobody
CMD ["python", "-u", "redirect_server.py"]
