---
name: workflow-automation
description: Use when building automations with n8n, Zapier, or Make. Covers workflow design, triggers, actions, error handling, and integration patterns for no-code/low-code automation.
summary: Workflow automation with n8n, Zapier, Make including triggers, actions, data transformation, error handling, and integration patterns.
triggers: [n8n, Zapier, Make, workflow, automation, trigger, integration, no-code, low-code]
disable-model-invocation: true

---
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

return items.map(item => {
  const data = item.json;
  
  return {
    json: {
      // Flatten nested objects
      customer_id: data.customer.id,
      customer_name: `${data.customer.firstName} ${data.customer.lastName}`,
      
      // Transform dates
      created_at: new Date(data.createdAt).toISOString(),
      
      // Calculate derived fields
      total_value: data.items.reduce((sum, item) => sum + item.price, 0),
      
      // Filter and transform arrays
      product_names: data.items
        .filter(item => item.status === 'active')
        .map(item => item.name)
        .join(', '),
    }
  };
});
```

### Conditional Branching
```json
{
  "name": "IF Customer Type",
  "type": "n8n-nodes-base.if",
  "parameters": {
    "conditions": {
      "string": [
        {
          "value1": "={{ $json.customer_type }}",
          "operation": "equals",
          "value2": "enterprise"
        }
      ]
    }
  }
}
```

## Common Workflow Patterns

### 1. Webhook → Process → Notify
```
[Webhook Trigger]
       ↓
[Validate Input]
       ↓
[Process Data]
       ↓
    ┌──┴──┐
    ↓     ↓
[Success] [Failure]
    ↓     ↓
[Notify] [Alert]
```

### 2. Scheduled Sync
```
[Schedule Trigger: Every hour]
       ↓
[Fetch from Source API]
       ↓
[Transform Data]
       ↓
[Upsert to Destination]
       ↓
[Log Results]
```

### 3. Event-Driven Pipeline
```
[Event Trigger: New Order]
       ↓
[Enrich with Customer Data]
       ↓
[Update Inventory]
       ↓
[Send Confirmation Email]
       ↓
[Update Analytics]
```

## Zapier Integration

### Multi-Step Zap
```
Trigger: New Row in Google Sheets
    ↓
Action 1: Find Contact in HubSpot
    ↓
Filter: Only continue if contact exists
    ↓
Action 2: Update Contact in HubSpot
    ↓
Action 3: Send Slack Notification
    ↓
Action 4: Log to Airtable
```

### Path Branching
```
Trigger: New Form Submission
    ↓
Paths by Field Value:
├── Path A (Type = "Bug"): Create Jira Issue
├── Path B (Type = "Feature"): Create Linear Issue  
└── Path C (Type = "Question"): Create Zendesk Ticket
```

### Formatter Patterns
```
Text:
- Capitalize: "john doe" → "John Doe"
- Extract Email: "Contact: john@example.com" → "john@example.com"
- Split Text: "a,b,c" → ["a", "b", "c"]

Numbers:
- Format Currency: 1234.5 → "$1,234.50"
- Random Number: → Random between 1-100

Dates:
- Format: "2024-01-15" → "January 15, 2024"
- Add/Subtract: Now + 7 days
- Compare: Is before/after
```

## Make (Integromat) Patterns

### Iterator for Arrays
```
[HTTP Request: Get Orders]
        ↓
[Iterator: Split Orders]
        ↓
[For Each Order]
   ├── [Get Customer]
   ├── [Update Status]
   └── [Send Email]
        ↓
[Array Aggregator: Collect Results]
        ↓
[Generate Report]
```

### Error Handling Routes
```
[Main Module]
    ├── Success Route → [Next Step]
    ├── Error Route → [Log Error] → [Retry Logic]
    └── Incomplete Route → [Handle Partial Data]
```

## Best Practices

### Rate Limiting
```javascript
// n8n: Add delay between API calls
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

const items = $input.all();
const results = [];

for (const item of items) {
  const result = await processItem(item);
  results.push(result);
  await delay(100); // 100ms between calls
}

return results;
```

### Deduplication
```javascript
// Track processed items to avoid duplicates
const processedIds = await getProcessedIds();
const items = $input.all();

const newItems = items.filter(item => !processedIds.includes(item.json.id));

if (newItems.length === 0) {
  return []; // No new items to process
}

// Mark as processed
await markAsProcessed(newItems.map(i => i.json.id));

return newItems;
```

### Chunking Large Datasets
```javascript
// Process in batches to avoid timeouts
const BATCH_SIZE = 100;
const items = $input.all();
const batches = [];

for (let i = 0; i < items.length; i += BATCH_SIZE) {
  batches.push(items.slice(i, i + BATCH_SIZE));
}

// Process each batch
for (const batch of batches) {
  await processBatch(batch);
}
```

## Monitoring & Alerting

### Execution Logging
```json
{
  "name": "Log Execution",
  "type": "n8n-nodes-base.httpRequest",
  "parameters": {
    "method": "POST",
    "url": "{{ $env.LOGGING_URL }}",
    "body": {
      "workflow_id": "={{ $workflow.id }}",
      "workflow_name": "={{ $workflow.name }}",
      "execution_id": "={{ $execution.id }}",
      "started_at": "={{ $execution.startTime }}",
      "items_processed": "={{ $items.length }}",
      "status": "success"
    }
  }
}
```

### Health Checks
```
[Schedule: Every 5 minutes]
       ↓
[Check Critical Services]
├── API Health Check
├── Database Connectivity
└── External Service Status
       ↓
[IF Any Failed]
       ↓
[Send Alert]
```

## Workflow Documentation Template

```markdown
# Workflow: [Name]

## Purpose
Brief description of what this workflow does.

## Trigger
- Type: Webhook / Schedule / Event
- Configuration: [Details]

## Steps
1. **[Step Name]**: [Description]
2. **[Step Name]**: [Description]

## Error Handling
- [How errors are handled]
- [Retry policy]
- [Alert destinations]

## Dependencies
- External APIs: [List]
- Credentials: [List]
- Other workflows: [List]

## Monitoring
- Dashboard: [Link]
- Alerts: [Configuration]

## Maintenance
- Owner: [Team/Person]
- Review schedule: [Frequency]
```

## Implementation Checklist
- [ ] Workflow purpose documented
- [ ] Error handling implemented
- [ ] Retry logic with backoff
- [ ] Rate limiting considered
- [ ] Idempotency ensured
- [ ] Logging at key points
- [ ] Alerts on failures
- [ ] Credentials stored securely
- [ ] Test execution successful
- [ ] Monitoring dashboard created
