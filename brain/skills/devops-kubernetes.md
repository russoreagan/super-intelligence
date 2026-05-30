# Kubernetes (Definitive)

## Goal
Create and operate production-ready Kubernetes deployments with repeatable, templated configuration across environments.

## When to Use
- Creating new Kubernetes Deployment/Service manifests
- Packaging applications with Helm charts
- Setting up GitOps workflows (ArgoCD/Flux)
- Implementing production best practices (probes, resources, security contexts)
- Managing multi-environment deployments

## Core Building Blocks

| Resource      | Purpose                                    |
| ------------- | ------------------------------------------ |
| Deployment    | Manages pods and replicas                  |
| Service       | Network abstraction (ClusterIP/LoadBalancer) |
| ConfigMap     | Non-sensitive configuration data           |
| Secret        | Sensitive data (credentials, keys)         |
| Ingress       | HTTP/HTTPS routing                         |
| PVC           | Persistent storage claims                  |

## Step-by-Step Workflow

### 1) Gather Requirements

**Questions to ask:**
- Stateless or stateful workload?
- Container image and tag?
- Ports and health check endpoints?
- Environment variables and configuration needs?
- Resource requirements (CPU, memory)?
- Storage requirements?
- Network exposure (internal/external)?
- Scaling requirements?

### 2) Create Deployment Manifest

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-app
  namespace: production
  labels:
    app: my-app
    version: "1.0.0"
spec:
  replicas: 3
  selector:
    matchLabels:
      app: my-app
  template:
    metadata:
      labels:
        app: my-app
        version: "1.0.0"
    spec:
      # Security context (run as non-root)
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        fsGroup: 1000
      containers:
      - name: my-app
        image: myregistry/my-app:1.0.0  # Never use :latest
        ports:
        - containerPort: 8080
          name: http
        # Resource limits (always set)
        resources:
          requests:
            memory: "256Mi"
            cpu: "250m"
          limits:
            memory: "512Mi"
            cpu: "500m"
        # Liveness probe (is container healthy?)
        livenessProbe:
          httpGet:
            path: /health
            port: http
          initialDelaySeconds: 30
          periodSeconds: 10
          failureThreshold: 3
        # Readiness probe (ready to receive traffic?)
        readinessProbe:
          httpGet:
            path: /ready
            port: http
          initialDelaySeconds: 5
          periodSeconds: 5
        # Container security
        securityContext:
          allowPrivilegeEscalation: false
          capabilities:
            drop: ["ALL"]
          readOnlyRootFilesystem: true
        # Environment variables
        env:
        - name: LOG_LEVEL
          value: "info"
        envFrom:
        - configMapRef:
            name: my-app-config
        - secretRef:
            name: my-app-secrets
```

### 3) Create Service Manifest
