---
name: cloud-infrastructure
description: Use when building cloud infrastructure with Terraform, optimizing costs, designing multi-cloud architectures, and implementing IaC best practices. Covers AWS, Azure, GCP patterns and resource management.
summary: Cloud infrastructure with Terraform modules, cost optimization, multi-cloud patterns, reserved instances, and IaC best practices.
triggers: [Terraform, AWS, Azure, GCP, cloud, infrastructure, cost optimization, IaC, multi-cloud]
disable-model-invocation: true

---
# Cloud Infrastructure (Unified)

## Goal
Build reliable, cost-effective cloud infrastructure using Infrastructure as Code with proper resource management and optimization.

## When to Use
- Creating Terraform modules
- Optimizing cloud costs
- Designing multi-cloud architectures
- Implementing IaC best practices
- Setting up cloud governance
- Managing cloud resources at scale

## Terraform Module Pattern

### Standard Module Structure
```
module-name/
├── main.tf          # Main resources
├── variables.tf     # Input variables
├── outputs.tf       # Output values
├── versions.tf      # Provider versions
├── README.md        # Documentation
├── examples/
│   └── complete/
│       └── main.tf
└── tests/
    └── module_test.go
```

### AWS VPC Module Example

**main.tf:**
```hcl
resource "aws_vpc" "main" {
  cidr_block           = var.cidr_block
  enable_dns_hostnames = var.enable_dns_hostnames
  enable_dns_support   = var.enable_dns_support

  tags = merge(
    { Name = var.name },
    var.tags
  )
}

resource "aws_subnet" "private" {
  count             = length(var.private_subnet_cidrs)
  vpc_id            = aws_vpc.main.id
  cidr_block        = var.private_subnet_cidrs[count.index]
  availability_zone = var.availability_zones[count.index]

  tags = merge(
    {
      Name = "${var.name}-private-${count.index + 1}"
      Tier = "private"
    },
    var.tags
  )
}

resource "aws_subnet" "public" {
  count                   = length(var.public_subnet_cidrs)
  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.public_subnet_cidrs[count.index]
  availability_zone       = var.availability_zones[count.index]
  map_public_ip_on_launch = true

  tags = merge(
    {
      Name = "${var.name}-public-${count.index + 1}"
      Tier = "public"
    },
    var.tags
  )
}

resource "aws_internet_gateway" "main" {
  count  = var.create_internet_gateway ? 1 : 0
  vpc_id = aws_vpc.main.id

  tags = merge(
    { Name = "${var.name}-igw" },
    var.tags
  )
}

resource "aws_nat_gateway" "main" {
  count         = var.enable_nat_gateway ? length(var.public_subnet_cidrs) : 0
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags = merge(
    { Name = "${var.name}-nat-${count.index + 1}" },
    var.tags
  )
}
```

**variables.tf:**
```hcl
variable "name" {
  description = "Name of the VPC"
  type        = string
}

variable "cidr_block" {
  description = "CIDR block for VPC"
  type        = string
  validation {
    condition     = can(cidrhost(var.cidr_block, 0))
    error_message = "Must be valid CIDR notation."
  }
}

variable "availability_zones" {
  description = "List of availability zones"
  type        = list(string)
}

variable "private_subnet_cidrs" {
  description = "CIDR blocks for private subnets"
  type        = list(string)
  default     = []
}

variable "public_subnet_cidrs" {
  description = "CIDR blocks for public subnets"
  type        = list(string)
  default     = []
}

variable "enable_nat_gateway" {
  description = "Enable NAT Gateway"
  type        = bool
  default     = true
}

variable "tags" {
  description = "Additional tags"
  type        = map(string)
  default     = {}
}
```

**outputs.tf:**
```hcl
output "vpc_id" {
  description = "ID of the VPC"
  value       = aws_vpc.main.id
}

output "private_subnet_ids" {
  description = "IDs of private subnets"
  value       = aws_subnet.private[*].id
}

output "public_subnet_ids" {
  description = "IDs of public subnets"
  value       = aws_subnet.public[*].id
}
```

## Cost Optimization

### Cost Optimization Framework

| Strategy         | AWS                      | Azure                   | GCP                     |
| ---------------- | ------------------------ | ----------------------- | ----------------------- |
| Reserved         | Reserved Instances       | Reserved VMs            | Committed Use           |
| Spot/Preemptible | Spot Instances           | Spot VMs                | Preemptible VMs         |
| Savings Plans    | Compute Savings Plans    | -                       | -                       |
| Right-sizing     | Trusted Advisor          | Azure Advisor           | Recommender             |
| Auto-scaling     | ASG, ECS, Lambda         | VMSS, AKS               | MIG, GKE                |

### AWS Cost Patterns

**Reserved Instances:**
```
Savings: 30-72% vs On-Demand
Term: 1 or 3 years
Payment: All/Partial/No upfront
Flexibility: Standard or Convertible
```

**Spot Instances:**
```
Savings: Up to 90%
Best for: Batch jobs, CI/CD, stateless workloads
Risk: 2-minute interruption notice
```

**S3 Lifecycle:**
```hcl
resource "aws_s3_bucket_lifecycle_configuration" "main" {
  bucket = aws_s3_bucket.main.id

  rule {
    id     = "archive-old-data"
    status = "Enabled"

    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }

    transition {
      days          = 90
      storage_class = "GLACIER"
    }

    expiration {
      days = 365
    }
  }
}
```

### Cost Tagging Strategy
```hcl
locals {
  required_tags = {
    Environment = var.environment
    Project     = var.project
    CostCenter  = var.cost_center
    Owner       = var.owner
    ManagedBy   = "terraform"
  }
}

# Apply to all resources
resource "aws_instance" "example" {
  # ... configuration
  tags = merge(local.required_tags, {
    Name = "example-instance"
  })
}
```

### Budget Alerts
```hcl
resource "aws_budgets_budget" "monthly" {
  name              = "monthly-budget"
  budget_type       = "COST"
  limit_amount      = "1000"
  limit_unit        = "USD"
  time_unit         = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = ["alerts@example.com"]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = ["alerts@example.com"]
  }
}
```

## Multi-Cloud Patterns

### Provider Configuration
```hcl
# versions.tf
terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.0"
    }
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = local.required_tags
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.azure_subscription_id
}

provider "google" {
  project = var.gcp_project
  region  = var.gcp_region
}
```

### Abstraction Layer
```hcl
# Generic compute module
module "compute" {
  source = "./modules/compute"

  provider    = var.cloud_provider  # aws, azure, gcp
  instance_type = var.instance_type
  count       = var.instance_count
  
  # Cloud-specific settings
  aws_config = var.cloud_provider == "aws" ? {
    ami           = var.aws_ami
    subnet_id     = var.aws_subnet_id
    key_name      = var.aws_key_name
  } : null
  
  azure_config = var.cloud_provider == "azure" ? {
    resource_group = var.azure_resource_group
    vnet_id        = var.azure_vnet_id
  } : null
}
```

## State Management

### Remote State
```hcl
terraform {
  backend "s3" {
    bucket         = "terraform-state-prod"
    key            = "infrastructure/vpc/terraform.tfstate"
    region         = "us-west-2"
    encrypt        = true
    dynamodb_table = "terraform-locks"
  }
}
```

### State Locking
```hcl
# DynamoDB table for state locking
resource "aws_dynamodb_table" "terraform_locks" {
  name         = "terraform-locks"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }
}
```

### Workspace Strategy
```bash
# Environment isolation via workspaces
terraform workspace new prod
terraform workspace new staging
terraform workspace select prod

# Reference in config
locals {
  environment = terraform.workspace
  instance_count = {
    prod    = 3
    staging = 1
  }[terraform.workspace]
}
```

## Security Patterns

### IAM Least Privilege
```hcl
# Application-specific IAM role
resource "aws_iam_role" "app" {
  name = "${var.app_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "ec2.amazonaws.com"
      }
    }]
  })
}

# Minimal permissions
resource "aws_iam_role_policy" "app" {
  name = "${var.app_name}-policy"
  role = aws_iam_role.app.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject"
        ]
        Resource = "${aws_s3_bucket.app.arn}/*"
      },
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = aws_secretsmanager_secret.app.arn
      }
    ]
  })
}
```

### Encryption
```hcl
# KMS key for encryption
resource "aws_kms_key" "main" {
  description             = "Main encryption key"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Enable IAM User Permissions"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      }
    ]
  })
}

# Encrypt S3 bucket
resource "aws_s3_bucket_server_side_encryption_configuration" "main" {
  bucket = aws_s3_bucket.main.id

  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.main.arn
      sse_algorithm     = "aws:kms"
    }
  }
}
```

## Implementation Checklist
- [ ] Terraform modules follow standard structure
- [ ] All resources have required tags
- [ ] Cost optimization reviewed (Reserved, Spot, Lifecycle)
- [ ] Budget alerts configured
- [ ] Remote state with locking enabled
- [ ] Workspace strategy for environments
- [ ] IAM follows least privilege principle
- [ ] Encryption enabled for data at rest
- [ ] Network security groups restrict access
- [ ] Modules have examples and tests
