# TruckWys Backend

> **New here? Read [`HANDOVER.md`](./HANDOVER.md)** — the full engineering handover
> (what TruckWys is, the flow, the AI, integrations, go-live config + cron, and how to run it).

TruckWys is a finance + data + AI backend for South African road-freight **carriers** —
fast-pay/factoring, AI quoting & risk scoring, collections, and financial intelligence.
It complements fleet-management/TMS software (no ops/routing). Built on Django + DRF.

## Models

The following models are included in the project:

1. **User**: Custom user model with roles (Admin, Dispatcher, Driver, Customer).
2. **Customer**: Stores customer details such as name, company, email, and address.
3. **Driver**: Manages driver profiles, licenses, and statuses.
4. **Vehicle**: Tracks vehicle details, maintenance, and statuses.
5. **VehicleLog**: Logs vehicle-related activities such as maintenance and costs.
6. **Load**: Represents loads assigned to drivers and vehicles.
7. **Quote**: Manages quotes for customers, including rates and statuses.
8. **Invoice**: Tracks invoices for loads and payments.
9. **Payment**: Records payments made by customers for invoices.
10. **Expense**: Tracks expenses related to vehicles, drivers, and other categories.
11. **Notification**: Manages notifications sent to users.
12. **Settlement**: Tracks settlements for drivers, including revenue and deductions.

## Features

- User management with role-based access control.
- Vehicle and driver management.
- Load and quote tracking.
- Invoice and payment processing.
- Notifications and settlements.
- REST API for integration with frontend applications.

## Requirements

- Python 3.10+
- Django 4.2+
- PostgreSQL (optional, SQLite is used by default)

## Installation

1. Clone the repository:
   ```bash
   git clone <repository-url>
   cd truckwys-backend
   ```

2. Create and activate a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Set up environment variables:
   Create a `.env` file in the project root and configure it as follows:
   ```
   SECRET_KEY=your-secret-key-here
   DEBUG=True
   ALLOWED_HOSTS=localhost,127.0.0.1
   DB_NAME=truckwys
   DB_USER=postgres
   DB_PASSWORD=your-password
   DB_HOST=localhost
   DB_PORT=5432
   CORS_ALLOWED_ORIGINS=http://localhost:3000,http://localhost:8000
   ```

5. Apply migrations:
   ```bash
   python manage.py makemigrations
   python manage.py migrate
   ```

6. Create a superuser:
   ```bash
   python manage.py createsuperuser
   ```

7. Run the development server:
   ```bash
   python manage.py runserver
   ```

## Project Structure

- `core/`: Contains the main app with models, views, serializers, and admin configurations.
- `config/`: Contains project-level settings and configurations.
- `db.sqlite3`: Default SQLite database file (ignored in `.gitignore`).
- `requirements.txt`: Python dependencies.

## API Documentation

### Base URL
```
http://localhost:8000/api/
```

### Authentication

All endpoints require authentication except for registration and login.

#### Register
```bash
POST /api/auth/register/
Content-Type: application/json

{
  "username": "john_doe",
  "email": "john@example.com",
  "password": "securepassword",
  "first_name": "John",
  "last_name": "Doe",
  "role": "DISPATCHER"
}
```

Response:
```json
{
  "token": "abc123xyz...",
  "user": {
    "id": 1,
    "username": "john_doe",
    "email": "john@example.com",
    ...
  }
}
```

#### Login
```bash
POST /api/auth/login/
Content-Type: application/json

{
  "username": "john_doe",
  "password": "securepassword"
}
```

Response:
```json
{
  "token": "abc123xyz...",
  "user": {
    "id": 1,
    "username": "john_doe",
    ...
  }
}
```

#### Logout
```bash
POST /api/auth/logout/
Authorization: Token abc123xyz...
```

#### Using the Token

Include the token in the Authorization header for all authenticated requests:

```bash
GET /api/vehicles/
Authorization: Token abc123xyz...
```

### API Endpoints

#### 1. Users
- **List all users**: `GET /api/users/`
- **Create user**: `POST /api/users/`
- **Get user details**: `GET /api/users/{id}/`
- **Update user**: `PUT /api/users/{id}/` or `PATCH /api/users/{id}/`
- **Delete user**: `DELETE /api/users/{id}/`
- **Filters**: `?role=ADMIN&is_active=true`
- **Search**: `?search=john`

#### 2. Customers
- **List all customers**: `GET /api/customers/`
- **Create customer**: `POST /api/customers/`
- **Get customer details**: `GET /api/customers/{id}/`
- **Update customer**: `PUT /api/customers/{id}/` or `PATCH /api/customers/{id}/`
- **Delete customer**: `DELETE /api/customers/{id}/`
- **Get customer loads**: `GET /api/customers/{id}/loads/`
- **Get customer invoices**: `GET /api/customers/{id}/invoices/`
- **Filters**: `?status=ACTIVE&city=NewYork&state=NY`
- **Search**: `?search=company_name`

#### 3. Drivers
- **List all drivers**: `GET /api/drivers/`
- **Create driver**: `POST /api/drivers/`
- **Get driver details**: `GET /api/drivers/{id}/`
- **Update driver**: `PUT /api/drivers/{id}/` or `PATCH /api/drivers/{id}/`
- **Delete driver**: `DELETE /api/drivers/{id}/`
- **Get driver loads**: `GET /api/drivers/{id}/loads/`
- **Get driver settlements**: `GET /api/drivers/{id}/settlements/`
- **Filters**: `?status=ACTIVE&license_state=CA`
- **Search**: `?search=license_number`

#### 4. Vehicles
- **List all vehicles**: `GET /api/vehicles/`
- **Create vehicle**: `POST /api/vehicles/`
- **Get vehicle details**: `GET /api/vehicles/{id}/`
- **Update vehicle**: `PUT /api/vehicles/{id}/` or `PATCH /api/vehicles/{id}/`
- **Delete vehicle**: `DELETE /api/vehicles/{id}/`
- **Get vehicle logs**: `GET /api/vehicles/{id}/logs/`
- **Get vehicle loads**: `GET /api/vehicles/{id}/loads/`
- **Filters**: `?status=ACTIVE&type=TRUCK&fuel_type=DIESEL`
- **Search**: `?search=plate_number`

#### 5. Vehicle Logs
- **List all logs**: `GET /api/vehicle-logs/`
- **Create log**: `POST /api/vehicle-logs/`
- **Get log details**: `GET /api/vehicle-logs/{id}/`
- **Update log**: `PUT /api/vehicle-logs/{id}/` or `PATCH /api/vehicle-logs/{id}/`
- **Delete log**: `DELETE /api/vehicle-logs/{id}/`
- **Filters**: `?vehicle={vehicle_id}&log_type=MAINTENANCE&date=2025-12-23`
- **Search**: `?search=description`

#### 6. Loads
- **List all loads**: `GET /api/loads/`
- **Create load**: `POST /api/loads/`
- **Get load details**: `GET /api/loads/{id}/`
- **Update load**: `PUT /api/loads/{id}/` or `PATCH /api/loads/{id}/`
- **Delete load**: `DELETE /api/loads/{id}/`
- **Update load status**: `PATCH /api/loads/{id}/update_status/`
  ```json
  {
    "status": "DELIVERED"
  }
  ```
- **Assign driver**: `POST /api/loads/{id}/assign_driver/`
  ```json
  {
    "driver_id": 1,
    "vehicle_id": 1
  }
  ```
- **Filters**: `?status=IN_TRANSIT&customer={customer_id}&driver={driver_id}&vehicle={vehicle_id}`
- **Search**: `?search=load_number`

#### 7. Quotes
- **List all quotes**: `GET /api/quotes/`
- **Create quote**: `POST /api/quotes/`
- **Get quote details**: `GET /api/quotes/{id}/`
- **Update quote**: `PUT /api/quotes/{id}/` or `PATCH /api/quotes/{id}/`
- **Delete quote**: `DELETE /api/quotes/{id}/`
- **Update quote status**: `PATCH /api/quotes/{id}/update_status/`
  ```json
  {
    "status": "ACCEPTED"
  }
  ```
- **Filters**: `?status=PENDING&customer={customer_id}`
- **Search**: `?search=quote_number`

#### 8. Invoices
- **List all invoices**: `GET /api/invoices/`
- **Create invoice**: `POST /api/invoices/`
- **Get invoice details**: `GET /api/invoices/{id}/`
- **Update invoice**: `PUT /api/invoices/{id}/` or `PATCH /api/invoices/{id}/`
- **Delete invoice**: `DELETE /api/invoices/{id}/`
- **Get invoice payments**: `GET /api/invoices/{id}/payments/`
- **Filters**: `?status=UNPAID&customer={customer_id}&load={load_id}`
- **Search**: `?search=invoice_number`

#### 9. Payments
- **List all payments**: `GET /api/payments/`
- **Create payment**: `POST /api/payments/`
- **Get payment details**: `GET /api/payments/{id}/`
- **Update payment**: `PUT /api/payments/{id}/` or `PATCH /api/payments/{id}/`
- **Delete payment**: `DELETE /api/payments/{id}/`
- **Filters**: `?payment_method=CREDIT_CARD&customer={customer_id}&invoice={invoice_id}`
- **Search**: `?search=reference_number`

#### 10. Expenses
- **List all expenses**: `GET /api/expenses/`
- **Create expense**: `POST /api/expenses/`
- **Get expense details**: `GET /api/expenses/{id}/`
- **Update expense**: `PUT /api/expenses/{id}/` or `PATCH /api/expenses/{id}/`
- **Delete expense**: `DELETE /api/expenses/{id}/`
- **Filters**: `?category=FUEL&vehicle={vehicle_id}&driver={driver_id}`
- **Search**: `?search=vendor`

#### 11. Settlements
- **List all settlements**: `GET /api/settlements/`
- **Create settlement**: `POST /api/settlements/`
- **Get settlement details**: `GET /api/settlements/{id}/`
- **Update settlement**: `PUT /api/settlements/{id}/` or `PATCH /api/settlements/{id}/`
- **Delete settlement**: `DELETE /api/settlements/{id}/`
- **Approve settlement**: `PATCH /api/settlements/{id}/approve/`
- **Mark as paid**: `PATCH /api/settlements/{id}/mark_paid/`
- **Filters**: `?status=PENDING&driver={driver_id}`
- **Search**: `?search=settlement_number`

#### 12. Notifications
- **List user notifications**: `GET /api/notifications/`
- **Create notification**: `POST /api/notifications/`
- **Get notification details**: `GET /api/notifications/{id}/`
- **Update notification**: `PUT /api/notifications/{id}/` or `PATCH /api/notifications/{id}/`
- **Delete notification**: `DELETE /api/notifications/{id}/`
- **Mark as read**: `PATCH /api/notifications/{id}/mark_read/`
- **Mark all as read**: `POST /api/notifications/mark_all_read/`
- **Filters**: `?type=LOAD_ASSIGNED&is_read=false`

### Request/Response Examples

#### Create a Customer
**Request:**
```json
POST /api/customers/
{
  "name": "John Doe",
  "company": "ABC Logistics",
  "email": "john@example.com",
  "phone": "+1234567890",
  "address": "123 Main St",
  "city": "New York",
  "state": "NY",
  "zip": "10001",
  "status": "ACTIVE"
}
```

**Response:**
```json
{
  "id": 1,
  "name": "John Doe",
  "company": "ABC Logistics",
  "email": "john@example.com",
  "phone": "+1234567890",
  "address": "123 Main St",
  "city": "New York",
  "state": "NY",
  "zip": "10001",
  "status": "ACTIVE",
  "created_at": "2025-12-23T10:00:00Z",
  "updated_at": "2025-12-23T10:00:00Z"
}
```

#### Assign Driver to Load
**Request:**
```json
POST /api/loads/1/assign_driver/
{
  "driver_id": 5,
  "vehicle_id": 3
}
```

**Response:**
```json
{
  "id": 1,
  "load_number": "LD-2025-001",
  "status": "ASSIGNED",
  "driver": 5,
  "vehicle": 3,
  "driver_name": "mike_driver",
  "vehicle_info": "Freightliner Cascadia - ABC123",
  ...
}
```

### Common Query Parameters

- **Pagination**: All list endpoints support pagination
  - `?page=1&page_size=20`
- **Ordering**: Sort by fields
  - `?ordering=-created_at` (descending)
  - `?ordering=name` (ascending)
- **Search**: Full-text search across specified fields
  - `?search=keyword`
- **Filtering**: Filter by specific fields
  - `?status=ACTIVE&role=DRIVER`

### Error Responses

**400 Bad Request:**
```json
{
  "error": "Invalid status"
}
```

**401 Unauthorized:**
```json
{
  "detail": "Authentication credentials were not provided."
}
```

**404 Not Found:**
```json
{
  "detail": "Not found."
}
```

## Admin Panel

The admin panel is available at `/admin/`. Use the superuser credentials to log in.

## Static and Media Files

- Static files are served from `/static/`.
- Media files are served from `/media/`.

## License

This project is licensed under the MIT License. See the LICENSE file for details.

## Contributing

Contributions are welcome! Please fork the repository and submit a pull request.