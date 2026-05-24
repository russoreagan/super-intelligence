---
name: kubernetes
description: Use when deploying services on Kubernetes including generating production-ready manifests, packaging with Helm charts, and operating GitOps workflows with ArgoCD or Flux.
summary: Kubernetes manifests, Helm charts, GitOps (ArgoCD/Flux), and production deployment best practices.
triggers: [Kubernetes, K8s, Helm, manifest, deployment, pod, service, GitOps, ArgoCD, Flux]
disable-model-invocation: true

---
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

**ClusterIP (internal only):**
```yaml
apiVersion: v1
kind: Service
metadata:
  name: my-app
  namespace: production
spec:
  type: ClusterIP
  selector:
    app: my-app
  ports:
  - name: http
    port: 80
    targetPort: 8080
```

**LoadBalancer (external access):**
```yaml
apiVersion: v1
kind: Service
metadata:
  name: my-app-external
  annotations:
    service.beta.kubernetes.io/aws-load-balancer-type: nlb
spec:
  type: LoadBalancer
  selector:
    app: my-app
  ports:
  - name: http
    port: 80
    targetPort: 8080
```

### 4) Create ConfigMap and Secret

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: my-app-config
  namespace: production
data:
  APP_MODE: production
  LOG_LEVEL: info
  DATABASE_HOST: db.example.com
---
apiVersion: v1
kind: Secret
metadata:
  name: my-app-secrets
  namespace: production
type: Opaque
stringData:
  DATABASE_PASSWORD: supersecret  # Use external secret manager in production
```

## Helm Chart Scaffolding

### Create New Chart
```bash
helm create my-app
```

### Standard Chart Structure
```
my-app/
├── Chart.yaml           # Chart metadata
├── values.yaml          # Default configuration
├── charts/              # Dependencies
├── templates/           # Kubernetes manifests
│   ├── _helpers.tpl     # Template helpers
│   ├── deployment.yaml
│   ├── service.yaml
│   ├── ingress.yaml
│   └── NOTES.txt
└── .helmignore
```

### Chart.yaml
```yaml
apiVersion: v2
name: my-app
description: My Application Helm Chart
type: application
version: 1.0.0       # Chart version
appVersion: "2.1.0"  # Application version

dependencies:
- name: postgresql
  version: "12.0.0"
  repository: "https://charts.bitnami.com/bitnami"
  condition: postgresql.enabled
```

### values.yaml Structure
```yaml
image:
  repository: myregistry/my-app
  tag: "1.0.0"
  pullPolicy: IfNotPresent

replicaCount: 3

service:
  type: ClusterIP
  port: 80
  targetPort: 8080

ingress:
  enabled: false
  className: nginx
  hosts:
  - host: app.example.com
    paths:
    - path: /
      pathType: Prefix

resources:
  requests:
    memory: "256Mi"
    cpu: "250m"
  limits:
    memory: "512Mi"
    cpu: "500m"

autoscaling:
  enabled: false
  minReplicas: 2
  maxReplicas: 10
  targetCPUUtilizationPercentage: 80
```

### Deployment Template with Values
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "my-app.fullname" . }}
  labels:
    {{- include "my-app.labels" . | nindent 4 }}
spec:
  {{- if not .Values.autoscaling.enabled }}
  replicas: {{ .Values.replicaCount }}
  {{- end }}
  selector:
    matchLabels:
      {{- include "my-app.selectorLabels" . | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "my-app.selectorLabels" . | nindent 8 }}
    spec:
      containers:
      - name: {{ .Chart.Name }}
        image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
        imagePullPolicy: {{ .Values.image.pullPolicy }}
        ports:
        - containerPort: {{ .Values.service.targetPort }}
        resources:
          {{- toYaml .Values.resources | nindent 10 }}
```

### Helm Commands
```bash
# Validate chart
helm lint my-app/

# Dry-run install
helm install my-app ./my-app --dry-run --debug

# Install with custom values
helm install my-app ./my-app -f values-prod.yaml -n production

# Upgrade
helm upgrade my-app ./my-app -f values-prod.yaml -n production

# Rollback
helm rollback my-app 1 -n production
```

## GitOps with ArgoCD

### OpenGitOps Principles
1. **Declarative**: Entire system described declaratively
2. **Versioned and Immutable**: Desired state stored in Git
3. **Pulled Automatically**: Agents pull desired state
4. **Continuously Reconciled**: Agents reconcile actual vs desired

### ArgoCD Application
```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: my-app
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/myorg/k8s-manifests
    targetRevision: HEAD
    path: apps/my-app/production
  destination:
    server: https://kubernetes.default.svc
    namespace: production
  syncPolicy:
    automated:
      prune: true      # Remove resources not in Git
      selfHeal: true   # Fix drift automatically
    syncOptions:
    - CreateNamespace=true
```

### GitOps Repository Structure
```
k8s-manifests/
├── apps/
│   ├── my-app/
│   │   ├── base/              # Shared manifests
│   │   │   ├── deployment.yaml
│   │   │   ├── service.yaml
│   │   │   └── kustomization.yaml
│   │   ├── staging/           # Staging overlays
│   │   │   └── kustomization.yaml
│   │   └── production/        # Production overlays
│   │       └── kustomization.yaml
│   └── another-app/
└── infrastructure/
    └── argocd/
```

## Production Best Practices

### Always Do
- Set resource requests and limits
- Implement liveness and readiness probes
- Use specific image tags (not `:latest`)
- Run containers as non-root
- Use NetworkPolicies for traffic control
- Store secrets in external secret managers
- Label resources consistently

### Avoid
- Privileged containers
- Host network/PID/IPC namespaces
- Writable root filesystem
- Running as root user
- Hardcoded secrets in manifests

## Implementation Checklist
- [ ] Deployment with resource limits set
- [ ] Liveness and readiness probes configured
- [ ] Security context (non-root, read-only filesystem)
- [ ] ConfigMaps for configuration, Secrets for sensitive data
- [ ] Service for network exposure
- [ ] Ingress if external access needed
- [ ] Helm chart created for templating
- [ ] GitOps workflow configured (ArgoCD/Flux)
- [ ] Rollback plan documented
- [ ] Multi-environment values files (staging, production)
