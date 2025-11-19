FROM eclipse-temurin:11-jre

LABEL maintainer="vicente.lineros_m@mail.udp.cl"
LABEL description="Adaptive Scheduler for Flink on Kubernetes"

WORKDIR /app

# Copy JAR
COPY target/flink-adaptive-scheduler-1.0.0.jar /app/scheduler.jar

# Create user
RUN useradd -m scheduler && \
    chown -R scheduler:scheduler /app

USER scheduler

# Run scheduler
ENTRYPOINT ["java", "-jar", "scheduler.jar"]
