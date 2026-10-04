# LifeLink — Complete Flask + SQLite Project

## Run
1. Create a virtual environment: `python -m venv .venv`
2. Activate it.
3. Install: `pip install -r requirements.txt`
4. Start: `python app.py`
5. Open: http://127.0.0.1:5000

SQLite database `lifelink.db` is created automatically on first run.

### Demo accounts
Password for all demo accounts: `LifeLink@123`

- donor@lifelink.local — donor
- recipient@lifelink.local — recipient
- hospital@lifelink.local — verified hospital
- camp@lifelink.local — camp organizer
- admin@lifelink.local — admin

## Implemented
- Secure password hashing and session login/logout
- Role-based access control
- Donor profile with blood group, last donation date and browser geolocation
- Blood request creation
- Blood compatibility filtering
- Configurable donation cooldown
- OSRM road distance + travel time, with haversine fallback if OSRM is unavailable
- Urgency-based radius and time-based radius expansion
- Duplicate invitation prevention
- Donor accept/reject commitment
- Request state transitions
- Verified donation history
- Hospital verification
- Organ pledges and hospital connections
- Camp scheduling and blood-group quota tracking based on verified camp donations
- Admin activity log
- Database-backed UI buttons and forms

OSRM is a public routing service. For production use, configure your own routing service and secure/limit external requests.
