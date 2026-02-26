# To do list app

A personal task and project management app built with Flask. Manage complex projects with dependency tracking, visualize task relationships, and plan your schedule — all in one place.

## Features

### Task Management
- **Kanban Board** — Tasks are automatically sorted into six status columns: Actions, Blocked, Awaiting, Goals, Recurring, and Complete
- **Dependency Tracking** — Link tasks together; blocked tasks surface automatically when their dependencies are incomplete
- **Recurring Tasks** — Set daily, weekly, monthly, or yearly schedules with custom intervals and end conditions
- **Fixed-Time Events** — Schedule tasks to specific dates and times
- **Deadlines** — Attach deadlines and track overdue tasks
- **Soft Delete with Undo** — Delete tasks with a toast notification and one-click undo

### Views
- **Kanban** — Color-coded columns for at-a-glance project status
- **Calendar** — FullCalendar view of events and recurring tasks with drag-and-drop rescheduling
- **Flowchart** — D3.js dependency graph showing task relationships; auto-reduces redundant edges; supports custom node positioning and edge routing

### Settings
- Set your name as the default task owner
- Configure weekly capacity (hours per weekday)
- Override capacity for specific dates

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.9+, Flask 3.1, Flask-SQLAlchemy |
| Database | SQLite (via SQLAlchemy) |
| Frontend | Vanilla JS, HTML5, CSS3 |
| Calendar | FullCalendar 6.1.8 |
| Graph | D3.js v7 |
| Fonts | Inter (Google Fonts) |

## Getting Started

**1. Clone the repo and navigate to the project directory:**
```bash
git clone https://github.com/codenamemick/to-do.git
cd project_manager
```

**2. Create and activate a virtual environment:**
```bash
python3 -m venv venv
source venv/bin/activate
```

**3. Install dependencies:**
```bash
pip install flask flask-sqlalchemy
```

**4. Run the app:**
```bash
python app.py
```

**5. Open your browser to `http://localhost:5000`**

The SQLite database (`instance/project.db`) is created automatically on first run.

## Project Structure

```
project_manager/
├── app.py                  # Flask routes and API endpoints
├── models.py               # SQLAlchemy models
├── templates/
│   ├── index.html          # Main single-page app
│   ├── settings.html       # Settings page
│   └── task_modal.html     # Task detail/edit modal
├── flowchart_positions.json # Saved flowchart node positions
└── instance/
    └── project.db          # SQLite database (auto-created)
```

## Data Models

- **Task** — Core task with status, scheduling, recurrence, and dependency relationships
- **TaskDependency** — Many-to-many join table linking tasks to their dependencies
- **CompletionRecord** — Time-tracked completion entries per task
- **AppSettings** — Single-row config for owner name and weekly capacity
- **DailyCapacityOverride** — Per-date capacity overrides
- **FlowchartNodePosition** — Custom x/y positions for flowchart nodes
- **FlowchartEdgeCustomization** — Custom port assignments and waypoints for edges
