# TruckWys Backend

TruckWys is a backend system for managing logistics operations, including users, vehicles, loads, invoices, payments, and more. This project is built using Django and Django REST Framework.

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

## API Endpoints

The API endpoints are prefixed with `/api/`. You can add more endpoints in `core/urls.py`.

## Admin Panel

The admin panel is available at `/admin/`. Use the superuser credentials to log in.

## Static and Media Files

- Static files are served from `/static/`.
- Media files are served from `/media/`.

## License

This project is licensed under the MIT License. See the LICENSE file for details.

## Contributing

Contributions are welcome! Please fork the repository and submit a pull request.

