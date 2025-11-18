FROM openjdk:11-jre-slim

LABEL maintainer="your.email@university.edu"
LABEL description="Adaptive Scheduler for Flink on Kubernetes"

WORKDIR /app

# Copy JAR
COPY target/flink-adaptive-scheduler-1.0.0.jar /app/scheduler.jar

# Create user
RUN useradd -m -u 1000 scheduler && \
    chown -R scheduler:scheduler /app

USER scheduler

# Run scheduler
ENTRYPOINT ["java", "-jar", "scheduler.jar"]