# Workflow Automation (Unified)

## Goal
Build reliable, maintainable automated workflows using visual workflow tools with proper error handling and monitoring.

## When to Use
- Automating repetitive tasks
- Integrating multiple services
- Building data pipelines
- Creating notification workflows
- Synchronizing data between systems
- Orchestrating business processes

## Workflow Design Principles

### 1. Design for Failure
- Every external call can fail
- Network issues are common
- Rate limits will be hit
- Implement retries with backoff

### 2. Idempotency
- Same input should produce same output
- Safe to retry workflows
- Use deduplication keys

### 3. Observability
- Log key decision points
- Track workflow execution time
- Alert on failures

## n8n Workflows

### Basic Workflow Structure
```json
{
  "name": "New Customer Onboarding",
  "nodes": [
    {
      "name": "Webhook Trigger",
      "type": "n8n-nodes-base.webhook",
      "parameters": {
        "path": "new-customer",
        "httpMethod": "POST",
        "responseMode": "onReceived"
      }
    },
    {
      "name": "Create CRM Contact",
      "type": "n8n-nodes-base.hubspot",
      "parameters": {
        "operation": "create",
        "resource": "contact",
        "email": "={{ $json.email }}",
        "additionalFields": {
          "firstName": "={{ $json.first_name }}",
          "lastName": "={{ $json.last_name }}"
        }
      }
    },
    {
      "name": "Send Welcome Email",
      "type": "n8n-nodes-base.sendgrid",
      "parameters": {
        "operation": "send",
        "to": "={{ $json.email }}",
        "templateId": "d-abc123",
        "dynamicTemplateData": {
          "name": "={{ $json.first_name }}"
        }
      }
    },
    {
      "name": "Notify Slack",
      "type": "n8n-nodes-base.slack",
      "parameters": {
        "channel": "#new-customers",
        "text": "New customer: {{ $json.email }}"
      }
    }
  ]
}
```

### Error Handling
```json
{
  "name": "Error Handler Node",
  "type": "n8n-nodes-base.errorTrigger",
  "parameters": {}
},
{
  "name": "Log Error",
  "type": "n8n-nodes-base.httpRequest",
  "parameters": {
    "method": "POST",
    "url": "https://logging.example.com/errors",
    "body": {
      "workflow": "={{ $workflow.name }}",
      "error": "={{ $json.error.message }}",
      "timestamp": "={{ $now.toISO() }}"
    }
  }
},
{
  "name": "Alert Team",
  "type": "n8n-nodes-base.slack",
  "parameters": {
    "channel": "#alerts",
    "text": "Workflow failed: {{ $workflow.name }}\nError: {{ $json.error.message }}"
  }
}
```

### Data Transformation
```javascript
// Code node for complex transformations
const items = $input.all();
