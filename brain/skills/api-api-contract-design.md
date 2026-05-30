# API Contract Design

OpenAPI(Swagger) 기반 스키마 우선 API 설계 스킬입니다.

## Core Principle

> **"코드보다 계약(Contract)이 먼저다."**
> **"프론트엔드와 백엔드가 동시에 개발할 수 있게 API를 먼저 정의한다."**

## Schema-First vs Code-First

| 접근법 | 장점 | 단점 |
|--------|------|------|
| **Schema-First** (권장) | 병렬 개발 가능, 명확한 계약 | 초기 설계 시간 필요 |
| Code-First | 빠른 시작 | 문서와 코드 불일치 위험 |

## OpenAPI 기본 구조

### `openapi.yaml`

```yaml
openapi: 3.1.0
info:
  title: My API
  version: 1.0.0
  description: API for My Application

servers:
  - url: https://api.example.com/v1
    description: Production
  - url: http://localhost:3000/api
    description: Development

paths:
  /users:
    get:
      summary: Get all users
      operationId: getUsers
      tags:
        - Users
      parameters:
        - name: page
          in: query
          schema:
            type: integer
            default: 1
        - name: limit
          in: query
          schema:
            type: integer
            default: 20
            maximum: 100
      responses:
        '200':
          description: Successful response
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/UserListResponse'
        '401':
          $ref: '#/components/responses/Unauthorized'

    post:
      summary: Create a new user
      operationId: createUser
      tags:
        - Users
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CreateUserRequest'
      responses:
        '201':
          description: User created
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/User'
        '400':
          $ref: '#/components/responses/BadRequest'
        '409':
          description: Email already exists
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Error'

  /users/{userId}:
    get:
      summary: Get user by ID
      operationId: getUserById
      tags:
        - Users
      parameters:
        - name: userId
          in: path
          required: true
          schema:
            type: string
            format: uuid
      responses:
        '200':
          description: Successful response
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/User'
        '404':
          $ref: '#/components/responses/NotFound'

components:
  schemas:
    User:
      type: object
      required:
        - id
        - email
        - name
        - createdAt
      properties:
        id:
          type: string
          format: uuid
        email:
          type: string
          format: email
        name:
          type: string
          minLength: 1
          maxLength: 100
        avatarUrl:
          type: string
          format: uri
          nullable: true
        createdAt:
          type: string
          format: date-time
        updatedAt:
          type: string
          format: date-time
