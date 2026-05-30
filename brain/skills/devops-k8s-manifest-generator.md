# Kubernetes Manifest Generator

Step-by-step guidance for creating production-ready Kubernetes manifests including Deployments, Services, ConfigMaps, Secrets, and PersistentVolumeClaims.

## Purpose

This skill provides comprehensive guidance for generating well-structured, secure, and production-ready Kubernetes manifests following cloud-native best practices and Kubernetes conventions.

## When to Use This Skill

Use this skill when you need to:
- Create new Kubernetes Deployment manifests
- Define Service resources for network connectivity
- Generate ConfigMap and Secret resources for configuration management
- Create PersistentVolumeClaim manifests for stateful workloads
- Follow Kubernetes best practices and naming conventions
- Implement resource limits, health checks, and security contexts
- Design manifests for multi-environment deployments

## Step-by-Step Workflow

### 1. Gather Requirements

**Understand the workload:**
- Application type (stateless/stateful)
- Container image and version
- Environment variables and configuration needs
- Storage requirements
- Network exposure requirements (internal/external)
- Resource requirements (CPU, memory)
- Scaling requirements
- Health check endpoints

**Questions to ask:**
- What is the application name and purpose?
- What container image and tag will be used?
- Does the application need persistent storage?
- What ports does the application expose?
- Are there any secrets or configuration files needed?
- What are the CPU and memory requirements?
- Does the application need to be exposed externally?

### 2. Create Deployment Manifest

**Follow this structure:**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: <app-name>
  namespace: <namespace>
  labels:
    app: <app-name>
    version: <version>
spec:
  replicas: 3
  selector:
    matchLabels:
      app: <app-name>
  template:
    metadata:
      labels:
        app: <app-name>
        version: <version>
    spec:
      containers:
      - name: <container-name>
        image: <image>:<tag>
        ports:
        - containerPort: <port>
          name: http
        resources:
          requests:
            memory: "256Mi"
            cpu: "250m"
          limits:
            memory: "512Mi"
            cpu: "500m"
        livenessProbe:
          httpGet:
            path: /health
            port: http
          initialDelaySeconds: 30
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /ready
            port: http
          initialDelaySeconds: 5
          periodSeconds: 5
        env:
        - name: ENV_VAR
          value: "value"
        envFrom:
        - configMapRef:
            name: <app-name>-config
        - secretRef:
            name: <app-name>-secret
```

**Best practices to apply:**
- Always set resource requests and limits
- Implement both liveness and readiness probes
- Use specific image tags (never `:latest`)
- Apply security context for non-root users
- Use labels for organization and selection
- Set appropriate replica count based on availability needs

**Reference:** See `references/deployment-spec.md` for detailed deployment options
