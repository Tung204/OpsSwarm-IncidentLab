FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "fastapi>=0.115,<1" "uvicorn[standard]>=0.30,<1" "pydantic>=2.8,<3" "requests>=2.32,<3"
COPY incidentlab ./incidentlab
ENV PYTHONPATH=/app
EXPOSE 8080
CMD ["uvicorn","incidentlab.app:app","--host","0.0.0.0","--port","8080"]
