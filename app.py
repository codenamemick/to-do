
from flask import Flask, request, render_template, redirect, jsonify
from models import db, Task, TaskDependency, CompletionRecord, StatusCache, FlowchartNodePosition, FlowchartEdgeCustomization
from datetime import datetime, date, timedelta
import calendar as cal
import json


app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///project.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)


@app.before_request
def clear_status_cache():
    """Clear the status cache at the start of each request."""
    StatusCache.clear()

# Sets default colours of statuses
def query_incomplete_tasks(exclude_id=None):
    """Return all non-deleted, incomplete tasks, optionally excluding one task by ID."""
    filters = [Task.deleted == False]
    if exclude_id is not None:
        filters.append(Task.id != exclude_id)
    tasks = Task.query.filter(*filters).all()
    return [t for t in tasks if t.status != 'Complete']


# Source of truth for status colors — keep in sync with :root variables in index.html
STATUS_COLORS = {
        'Free':       '#2196f3',
        'Blocked':    '#c62828',
        'Awaiting':   '#757575',
        'Event':      '#da8f1f',
        'Goal':       '#9300ef',
        'Complete':   '#4caf50',
    }

def sort_actions_tasks(tasks):
    """
    Sort tasks for the Actions column with complex ordering:
    1. Include Awaiting and Blocked tasks temporarily (to push their dependents down)
    2. Sort by status (Free first, then Awaiting/Blocked)
    3. Then by number of dependents DESC (tasks with more dependents ranked higher)
    4. Then by number of dependencies ASC (fewer dependencies ranked higher)
    5. Constraint: A task must never be ranked higher than any of its dependencies
    6. Remove Awaiting, Blocked, and Event tasks from final result (only Free remains)

    Uses topological sort with priority ordering.
    """
    # Include Free, Blocked, Awaiting, and Event for the sort
    # (Awaiting, Blocked, Event removed at the end - only Free stays in Actions)
    action_tasks = [t for t in tasks if t.status in ('Free', 'Blocked', 'Awaiting', 'Event')]

    if not action_tasks:
        return []

    # Build dependency graph for action tasks only
    task_by_id = {t.id: t for t in action_tasks}
    action_task_ids = set(task_by_id.keys())

    # For each task, get its dependencies that are also in the action tasks
    deps_in_actions = {}
    for task in action_tasks:
        deps_in_actions[task.id] = set()
        for dep in task.dependencies:
            if dep.depends_on_id in action_task_ids:
                deps_in_actions[task.id].add(dep.depends_on_id)

    # Calculate priority score: (status, dependents DESC, dependencies ASC)
    # Higher score = should appear earlier
    # Status: Free = 0, Awaiting/Blocked = 1 (so Awaiting/Blocked sorts later)
    def get_priority(task):
        status_order = 0 if task.status == 'Free' else 1
        # Negate downstream_count for DESC, use upstream_count as-is for ASC
        return (status_order, -task.downstream_count, task.upstream_count, task.title.lower())

    # Topological sort with priority
    result = []
    remaining = set(action_task_ids)

    while remaining:
        # Find tasks whose dependencies (within action tasks) are all satisfied
        available = []
        for task_id in remaining:
            deps = deps_in_actions[task_id]
            if deps.issubset(set(t.id for t in result)):
                available.append(task_by_id[task_id])

        if not available:
            # Circular dependency or error - just add remaining by priority
            remaining_tasks = [task_by_id[tid] for tid in remaining]
            remaining_tasks.sort(key=get_priority)
            result.extend(remaining_tasks)
            break

        # Sort available tasks by priority and pick the best one
        available.sort(key=get_priority)
        best = available[0]
        result.append(best)
        remaining.remove(best.id)

    # Remove Awaiting, Blocked, and Event tasks - only Free tasks stay in Actions column
    result = [t for t in result if t.status == 'Free']

    return result


def sort_tasks_by_status(tasks, status):
    """
    Sort tasks for the Awaiting or Blocked column:
    Sort by dependencies ASC, then dependents DESC, then title.
    """
    filtered = [t for t in tasks if t.status == status]

    if not filtered:
        return []

    filtered.sort(key=lambda t: (t.upstream_count, -t.downstream_count, t.title.lower()))
    return filtered


# Home
@app.route("/")
def home():
    tasks = Task.query.filter_by(deleted=False).all()

    # Sort action tasks with complex ordering
    actions_sorted = sort_actions_tasks(tasks)

    # Sort awaiting and blocked tasks
    awaiting_sorted = sort_tasks_by_status(tasks, 'Awaiting')
    blocked_sorted = sort_tasks_by_status(tasks, 'Blocked')

    # Sort goal tasks by goal-dependency count (fewest first)
    goals_sorted = sorted(
        [t for t in tasks if t.status == 'Goal'],
        key=lambda t: (
            t.get_upstream_goal_count(),  # fewest goal dependencies first
            -t.get_downstream_goal_count(),  # most goal dependents first
            t.title.lower()               # alphabetical tie-breaker
        )
    )

    # Generate calendar events including recurring instances
    calendar_events = []
    today = datetime.now()
    start_range = today - timedelta(days=30)  # Show past month
    end_range = today + timedelta(days=365)   # Show next year

    for task in tasks:
        if not (task.status == 'Event' or task.recurring):
            continue

        task_color = STATUS_COLORS.get(task.status, '#2196f3')

        if task.recurring:
            # Generate recurring instances
            instances = generate_recurring_events(task, start_range, end_range)
            for instance_date in instances:
                calendar_events.append({
                    'id': task.id,
                    'title': task.title,
                    'start': instance_date.isoformat(),
                    'color': task_color,
                })
        else:
            # Single event
            calendar_events.append({
                'id': task.id,
                'title': task.title,
                'start': task.scheduled_datetime.isoformat(),
                'color': task_color,
            })

    return render_template("index.html", tasks=tasks, actions_sorted=actions_sorted, awaiting_sorted=awaiting_sorted, blocked_sorted=blocked_sorted, goals_sorted=goals_sorted, calendar_events=calendar_events, now=datetime.now())

def add_months(dt, months):
    """Add months to a datetime, handling month-end edge cases."""
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    day = min(dt.day, cal.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def generate_recurring_events(task, start_date, end_date):
    """Generate recurring event instances for a task within a date range."""
    if not task.recurring or not task.scheduled_datetime:
        return []

    events = []
    current = task.scheduled_datetime
    count = 0
    max_count = task.recurrence_end_count if task.recurrence_end_type == 'after' else 365  # Limit to prevent infinite loops
    end_recurrence = task.recurrence_end_date if task.recurrence_end_type == 'on_date' else None

    frequency = task.recurrence_frequency or 'daily'
    interval = task.recurrence_interval or 1

    while current <= end_date and count < max_count:
        if current >= start_date:
            # For weekly recurrence, check if the day is in the selected days
            if frequency == 'weekly' and task.recurrence_days:
                selected_days = [int(d) for d in task.recurrence_days.split(',')]
                if current.weekday() in selected_days:
                    events.append(current)
            else:
                events.append(current)

        # Calculate next occurrence
        if frequency == 'daily':
            current = current + timedelta(days=interval)
        elif frequency == 'weekly':
            current = current + timedelta(days=1)  # Check each day for weekly
        elif frequency == 'monthly':
            current = add_months(current, interval)
        elif frequency == 'yearly':
            current = add_months(current, interval * 12)
        else:
            break

        count += 1

        # Check end conditions
        if end_recurrence and current.date() > end_recurrence:
            break

    return events


def parse_recurrence_days(form):
    """Parse selected weekdays from form checkboxes."""
    days = []
    for i in range(7):
        if form.get(f"recurrence_day_{i}"):
            days.append(str(i))
    return ",".join(days) if days else None


def apply_recurrence_fields(task, form):
    """Apply recurrence fields from form data onto a task object."""
    task.recurrence_frequency = form.get("recurrence_frequency", "daily")
    task.recurrence_interval = int(form.get("recurrence_interval", 1))
    task.recurrence_days = parse_recurrence_days(form)
    task.recurrence_month_type = form.get("recurrence_month_type", "day_of_month")
    task.recurrence_end_type = form.get("recurrence_end_type", "never")

    if task.recurrence_end_type == "after":
        task.recurrence_end_count = int(form.get("recurrence_end_count", 10))
        task.recurrence_end_date = None
    elif task.recurrence_end_type == "on_date" and form.get("recurrence_end_date"):
        task.recurrence_end_date = date.fromisoformat(form["recurrence_end_date"])
        task.recurrence_end_count = None
    else:
        task.recurrence_end_count = None
        task.recurrence_end_date = None


def parse_task_fields(form):
    """Parse common task fields from form data, returning a dict of field values."""
    scheduled_datetime = datetime.fromisoformat(form["scheduled_datetime"]) if form.get("scheduled_datetime") else None
    is_fixed_time = bool(form.get("fixed_time")) and scheduled_datetime is not None
    return {
        "title": form["title"],
        "description": form.get("description"),
        "estimated_duration": float(form["estimated_duration"]) if form.get("estimated_duration") else None,
        "unknown_dependencies": bool(form.get("unknown_dependencies")),
        "awaiting": bool(form.get("awaiting")),
        "recurring": bool(form.get("recurring")),
        "scheduled_datetime": scheduled_datetime,
        "fixed_time": is_fixed_time,
        "deadline": datetime.fromisoformat(form["deadline"]) if form.get("deadline") else None,
    }


# Create task
@app.route("/create-task", methods=["POST"])
def create_task():
    fields = parse_task_fields(request.form)
    task = Task(**fields, created_datetime=datetime.now())

    if task.recurring:
        apply_recurrence_fields(task, request.form)

    db.session.add(task)
    db.session.commit()

    # Add dependencies if any were selected (tasks this new task depends on)
    dependency_ids = request.form.getlist("dependency_ids")
    for dep_id in dependency_ids:
        if dep_id:
            dependency = TaskDependency(task_id=task.id, depends_on_id=int(dep_id))
            db.session.add(dependency)

    # Add dependents if any were selected (tasks that depend on this new task)
    dependent_ids = request.form.getlist("dependent_ids")
    for dep_id in dependent_ids:
        if dep_id:
            dependency = TaskDependency(task_id=int(dep_id), depends_on_id=task.id)
            db.session.add(dependency)

    db.session.commit()

    # If AJAX request, return JSON instead of redirect
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"success": True, "task_id": task.id, "task_title": task.title})

    # Redirect back to the view the user was on
    redirect_view = request.form.get("redirect_view", "kanban")
    return redirect(f"/?view={redirect_view}")


# Duplicate task
@app.route("/task/<int:task_id>/duplicate", methods=["POST"])
def duplicate_task(task_id):
    """Duplicate a task via AJAX, copying its properties and dependencies."""
    task = Task.query.get_or_404(task_id)
    new_task = Task(
        title=task.title + " (copy)",
        description=task.description,
        estimated_duration=task.estimated_duration,
        unknown_dependencies=task.unknown_dependencies,
        awaiting=task.awaiting,
        scheduled_datetime=task.scheduled_datetime,
        fixed_time=task.fixed_time,
        deadline=task.deadline,
        created_datetime=datetime.now(),
    )
    db.session.add(new_task)
    db.session.flush()  # Get the new ID

    # Copy dependencies (tasks this task depends on)
    for dep in task.dependencies:
        db.session.add(TaskDependency(task_id=new_task.id, depends_on_id=dep.depends_on_id))

    # Copy dependents (tasks that depend on this task)
    for dep in TaskDependency.query.filter_by(depends_on_id=task_id).all():
        db.session.add(TaskDependency(task_id=dep.task_id, depends_on_id=new_task.id))

    # Copy node position (offset slightly so it's visible)
    pos = FlowchartNodePosition.query.filter_by(task_id=task_id).first()
    if pos:
        new_pos = FlowchartNodePosition(task_id=new_task.id, x=pos.x + 50, y=pos.y + 50)
        db.session.add(new_pos)

    db.session.commit()
    return jsonify({"success": True, "task_id": new_task.id, "task_title": new_task.title})


# Quick delete task
@app.route("/task/<int:task_id>/quick-delete", methods=["POST"])
def quick_delete_task(task_id):
    """Soft delete a task via AJAX."""
    task = Task.query.get_or_404(task_id)
    task.deleted = True
    db.session.commit()
    return jsonify({"success": True, "task_id": task_id, "task_title": task.title})

# Undo delete 
@app.route("/task/<int:task_id>/undo-delete", methods=["POST"])
def undo_delete_task(task_id):
    """Restore a soft-deleted task."""
    task = Task.query.get_or_404(task_id)
    task.deleted = False
    db.session.commit()
    return jsonify({"success": True})

# View task
@app.route("/task/<int:task_id>")
def view_task(task_id):
    task = Task.query.get_or_404(task_id)
    all_tasks = Task.query.filter_by(deleted=False).all()
    return render_template("task_modal.html", task=task, all_tasks=all_tasks)


@app.route("/task/<int:task_id>/reschedule", methods=["POST"])
def reschedule_task(task_id):
    """Update task scheduled datetime (for calendar drag-drop)."""
    task = Task.query.get_or_404(task_id)
    data = request.get_json()
    if data and "scheduled_datetime" in data:
        task.scheduled_datetime = datetime.fromisoformat(data["scheduled_datetime"].replace('Z', '+00:00'))
        db.session.commit()
        return jsonify({"success": True})
    return jsonify({"error": "Missing scheduled_datetime"}), 400

# Update task 
@app.route("/update-task/<int:task_id>", methods=["POST"])
def update_task(task_id):
    task = Task.query.get_or_404(task_id)
    fields = parse_task_fields(request.form)
    for key, value in fields.items():
        setattr(task, key, value)

    if task.recurring:
        apply_recurrence_fields(task, request.form)
    else:
        # Clear recurring fields if no longer recurring
        for field in ('recurrence_frequency', 'recurrence_interval', 'recurrence_days',
                      'recurrence_month_type', 'recurrence_end_type',
                      'recurrence_end_count', 'recurrence_end_date'):
            setattr(task, field, None)

    db.session.commit()

    # If AJAX request, return JSON instead of redirect
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"success": True, "task_id": task.id, "task_title": task.title})

    return redirect("/")


# --- Dependency Management ---

# Add dependency
@app.route("/task/<int:task_id>/add-dependency", methods=["POST"])
def add_dependency(task_id):
    """Add a dependency to a task."""
    task = Task.query.get_or_404(task_id)
    depends_on_id = int(request.form["depends_on_id"])

    # Prevent self-dependency
    if depends_on_id == task_id:
        return jsonify({"error": "A task cannot depend on itself"}), 400

    # Check if dependency already exists
    existing = TaskDependency.query.filter_by(
        task_id=task_id, depends_on_id=depends_on_id
    ).first()
    if existing:
        return jsonify({"error": "Dependency already exists"}), 400

    dependency = TaskDependency(task_id=task_id, depends_on_id=depends_on_id)
    db.session.add(dependency)
    db.session.commit()

    return jsonify({"success": True})

# Remove dependency
@app.route("/task/<int:task_id>/remove-dependency/<int:depends_on_id>", methods=["POST"])
def remove_dependency(task_id, depends_on_id):
    """Remove a dependency from a task."""
    dependency = TaskDependency.query.filter_by(
        task_id=task_id, depends_on_id=depends_on_id
    ).first_or_404()

    db.session.delete(dependency)
    db.session.commit()

    return jsonify({"success": True})

# --- Task Completion ---

@app.route("/task/<int:task_id>/quick-complete", methods=["POST"])
def quick_complete_task(task_id):
    """Mark a task as complete with current time as start/end (for drag-drop)."""
    task = Task.query.get_or_404(task_id)

    # Check for incomplete dependencies
    if task.incomplete_dependencies:
        return jsonify({
            "error": "Cannot complete task with incomplete dependencies",
            "incomplete": [t.title for t in task.incomplete_dependencies]
        }), 400

    # Create a completion record with current time
    now = datetime.now()
    record = CompletionRecord(
        task_id=task_id,
        start_time=now,
        end_time=now,
        interval_number=1
    )
    db.session.add(record)
    db.session.commit()
    return jsonify({"success": True, "task_id": task_id, "task_title": task.title})

# Undo complete
@app.route("/task/<int:task_id>/undo-complete", methods=["POST"])
def undo_complete_task(task_id):
    """Remove all completion records for a task (undo complete)."""
    task = Task.query.get_or_404(task_id)

    # Delete all completion records for this task
    CompletionRecord.query.filter_by(task_id=task_id).delete()
    db.session.commit()

    return jsonify({"success": True})

# Set goal
@app.route("/task/<int:task_id>/set-goal", methods=["POST"])
def set_task_as_goal(task_id):
    """Set a task's unknown_dependencies to true, making it a Goal."""
    task = Task.query.get_or_404(task_id)
    task.unknown_dependencies = True
    db.session.commit()
    return jsonify({"success": True})


# --- Dependencies Data API ---

@app.route("/api/task/<int:task_id>/dependencies-data")
def get_dependencies_data(task_id):
    """Return all incomplete tasks with info on whether they are dependencies of the given task."""
    task = Task.query.get_or_404(task_id)

    incomplete_tasks = query_incomplete_tasks(exclude_id=task_id)

    # IDs of tasks this task depends on
    dependency_ids = {dep.depends_on_id for dep in task.dependencies}

    tasks_data = []
    for t in incomplete_tasks:
        tasks_data.append({
            'id': t.id,
            'title': t.title,
            'status': t.status,
            'is_dependency': t.id in dependency_ids
        })

    # Sort: dependencies first, then by status rank, then title
    tasks_data.sort(key=lambda t: (not t['is_dependency'], t['title'].lower()))

    return jsonify({'tasks': tasks_data})



# --- Dependents Data API ---

@app.route("/api/task/<int:task_id>/dependents-data")
def get_dependents_data(task_id):
    """Return all incomplete tasks with info on whether they depend on the given task."""
    target_task = Task.query.get_or_404(task_id)

    incomplete_tasks = query_incomplete_tasks(exclude_id=task_id)

    # Get IDs of tasks that directly depend on the target task
    dependent_ids = {dep.task_id for dep in target_task.dependents}

    tasks_data = []
    for task in incomplete_tasks:
        tasks_data.append({
            'id': task.id,
            'title': task.title,
            'status': task.status,
            'is_dependent': task.id in dependent_ids
        })

    # Sort: dependents first, then alphabetically by title
    tasks_data.sort(key=lambda t: (not t['is_dependent'], t['title'].lower()))

    return jsonify({'tasks': tasks_data})


@app.route("/api/tasks/incomplete")
def get_incomplete_tasks():
    """Return all incomplete, non-deleted tasks for dependency selection."""
    incomplete_tasks = query_incomplete_tasks()

    tasks_data = []
    for task in incomplete_tasks:
        tasks_data.append({
            'id': task.id,
            'title': task.title,
            'status': task.status
        })

    # Sort alphabetically by title
    tasks_data.sort(key=lambda t: t['title'].lower())

    return jsonify({'tasks': tasks_data})


@app.route("/api/search-tasks")
def search_tasks():
    """Search tasks by title, returning matches for the search bar."""
    query = request.args.get('q', '').strip().lower()

    # Get all non-deleted, incomplete tasks
    incomplete_tasks = query_incomplete_tasks()

    # Filter tasks that match the query (case-insensitive title search)
    if query:
        matching_tasks = [t for t in incomplete_tasks if query in t.title.lower()]
    else:
        matching_tasks = incomplete_tasks

    # Sort by status order: Free, Blocked, Event, Awaiting, Goal
    status_order = {'Free': 0, 'Blocked': 1, 'Event': 2, 'Awaiting': 3, 'Goal': 4}
    matching_tasks.sort(key=lambda t: (status_order.get(t.status, 99), t.title.lower()))

    results = [{
        'id': t.id,
        'title': t.title,
        'status': t.status
    } for t in matching_tasks]

    return jsonify(results)


# --- Dependency Graph ---
@app.route("/api/graph-data")
def graph_data():
    """Return nodes and edges for the dependency graph.

    Only includes trees where the final node(s) are Goals.
    Excludes complete and deleted tasks.
    Performs transitive reduction to show only essential edges.
    """
    tasks = Task.query.filter_by(deleted=False).all()
    task_by_id = {task.id: task for task in tasks}

    # Find all Goal tasks (these are the roots of our trees going backwards)
    goal_tasks = [task for task in tasks if task.status == 'Goal']

    # Traverse backwards from Goals to find all tasks in their dependency chains
    def collect_upstream_tasks(task, collected):
        """Recursively collect all incomplete, non-deleted dependencies."""
        for dep in task.dependencies:
            dep_task = task_by_id.get(dep.depends_on_id)
            if dep_task is None:  # Deleted task
                continue
            if dep_task.status == 'Complete':
                continue
            if dep_task.id in collected:
                continue
            collected.add(dep_task.id)
            collect_upstream_tasks(dep_task, collected)

    # Collect all task IDs that should be in the graph
    tasks_in_goal_trees = set()
    for goal in goal_tasks:
        tasks_in_goal_trees.add(goal.id)
        collect_upstream_tasks(goal, tasks_in_goal_trees)

    # Build adjacency list for tasks in the graph (dependency -> list of dependents)
    # adjacency[A] contains B means B depends on A (edge from A to B)
    adjacency = {task_id: [] for task_id in tasks_in_goal_trees}
    all_edges = []  # (from_id, to_id) where to_id depends on from_id

    for task_id in tasks_in_goal_trees:
        task = task_by_id[task_id]
        for dep in task.dependencies:
            if dep.depends_on_id in tasks_in_goal_trees:
                adjacency[dep.depends_on_id].append(task_id)
                all_edges.append((dep.depends_on_id, task_id))

    # Transitive reduction: remove edge A->C if there's a longer path A->...->C
    def can_reach_without_direct(start, end, excluded_edge):
        """Check if 'end' is reachable from 'start' without using the direct edge."""
        visited = set()
        stack = [start]
        while stack:
            current = stack.pop()
            if current == end and current != start:
                return True
            if current in visited:
                continue
            visited.add(current)
            for neighbor in adjacency[current]:
                # Skip the direct edge we're testing
                if current == excluded_edge[0] and neighbor == excluded_edge[1]:
                    continue
                if neighbor not in visited:
                    stack.append(neighbor)
        return False

    # Identify essential vs redundant edges
    essential_edge_set = set()
    for from_id, to_id in all_edges:
        if not can_reach_without_direct(from_id, to_id, (from_id, to_id)):
            essential_edge_set.add((from_id, to_id))

    # Load custom node positions
    node_positions = {np.task_id: {'x': np.x, 'y': np.y}
                      for np in FlowchartNodePosition.query.all()}

    # Load edge customizations
    edge_customs = {(ec.from_task_id, ec.to_task_id): ec
                    for ec in FlowchartEdgeCustomization.query.all()}

    nodes = []
    for task_id in tasks_in_goal_trees:
        task = task_by_id[task_id]
        node_data = {
            'id': task.id,
            'label': task.title,
            'color': STATUS_COLORS.get(task.status, '#999999'),
            'status': task.status,
            'hasUnknownDeps': task.unknown_dependencies
        }
        # Include custom position if exists
        if task_id in node_positions:
            node_data['customX'] = node_positions[task_id]['x']
            node_data['customY'] = node_positions[task_id]['y']
        nodes.append(node_data)

    edges = []
    for from_id, to_id in all_edges:
        to_task = task_by_id[to_id]
        edge_data = {
            'from': from_id,
            'to': to_id,
            'dashes': to_task.status == 'Goal',
            'redundant': (from_id, to_id) not in essential_edge_set
        }
        # Include edge customization if exists
        ec = edge_customs.get((from_id, to_id))
        if ec:
            edge_data['sourcePort'] = ec.source_port
            edge_data['targetPort'] = ec.target_port
            if ec.waypoints_json:
                edge_data['waypoints'] = json.loads(ec.waypoints_json)
        edges.append(edge_data)

    return jsonify({'nodes': nodes, 'edges': edges})


@app.route("/api/create-dependency", methods=["POST"])
def create_dependency_api():
    """Create a dependency between two tasks (for flowchart drag-and-drop).

    from_id: the task that will be depended upon
    to_id: the task that will depend on from_id
    """
    data = request.get_json()
    from_id = data.get('from_id')
    to_id = data.get('to_id')

    if not from_id or not to_id:
        return jsonify({'error': 'Missing from_id or to_id'}), 400

    if from_id == to_id:
        return jsonify({'error': 'Cannot create self-dependency'}), 400

    # Check if dependency already exists
    existing = TaskDependency.query.filter_by(task_id=to_id, depends_on_id=from_id).first()
    if existing:
        return jsonify({'error': 'Dependency already exists'}), 400

    # Check for circular dependency (to_id should not be in from_id's dependency chain)
    from_task = Task.query.get(from_id)
    to_task = Task.query.get(to_id)

    if not from_task or not to_task:
        return jsonify({'error': 'Task not found'}), 404

    # Simple circular check: see if from_id depends on to_id (directly or indirectly)
    def has_dependency_on(task, target_id, visited=None):
        if visited is None:
            visited = set()
        if task.id in visited:
            return False
        visited.add(task.id)
        for dep in task.dependencies:
            if dep.depends_on_id == target_id:
                return True
            dep_task = Task.query.get(dep.depends_on_id)
            if dep_task and has_dependency_on(dep_task, target_id, visited):
                return True
        return False

    if has_dependency_on(from_task, to_id):
        return jsonify({'error': 'Would create circular dependency'}), 400

    dependency = TaskDependency(task_id=to_id, depends_on_id=from_id)
    db.session.add(dependency)
    db.session.commit()

    return jsonify({'success': True})


# --- Flowchart Customization ---

@app.route("/api/flowchart/node-position", methods=["POST"])
def save_node_position():
    """Save custom node position in flowchart."""
    data = request.get_json()
    task_id = data.get('task_id')
    x = data.get('x')
    y = data.get('y')

    if not task_id or x is None or y is None:
        return jsonify({'error': 'Missing task_id, x, or y'}), 400

    # Check task exists
    task = Task.query.get(task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404

    # Update or create position
    position = FlowchartNodePosition.query.filter_by(task_id=task_id).first()
    if position:
        position.x = x
        position.y = y
    else:
        position = FlowchartNodePosition(task_id=task_id, x=x, y=y)
        db.session.add(position)

    db.session.commit()
    return jsonify({'success': True})


@app.route("/api/flowchart/node-position/<int:task_id>", methods=["DELETE"])
def reset_node_position(task_id):
    """Reset node position to auto-layout."""
    position = FlowchartNodePosition.query.filter_by(task_id=task_id).first()
    if position:
        db.session.delete(position)
        db.session.commit()
    return jsonify({'success': True})


@app.route("/api/flowchart/edge-customization", methods=["POST"])
def save_edge_customization():
    """Save custom edge routing (ports and waypoints)."""
    data = request.get_json()
    from_id = data.get('from_id')
    to_id = data.get('to_id')

    if not from_id or not to_id:
        return jsonify({'error': 'Missing from_id or to_id'}), 400

    # Update or create customization
    custom = FlowchartEdgeCustomization.query.filter_by(
        from_task_id=from_id, to_task_id=to_id
    ).first()

    if not custom:
        custom = FlowchartEdgeCustomization(from_task_id=from_id, to_task_id=to_id)
        db.session.add(custom)

    if 'source_port' in data:
        custom.source_port = data['source_port']
    if 'target_port' in data:
        custom.target_port = data['target_port']
    if 'waypoints' in data:
        custom.waypoints_json = json.dumps(data['waypoints']) if data['waypoints'] else None

    db.session.commit()
    return jsonify({'success': True})


@app.route("/api/flowchart/edge-customization", methods=["DELETE"])
def reset_edge_customization():
    """Reset edge to auto-routing."""
    data = request.get_json()
    from_id = data.get('from_id')
    to_id = data.get('to_id')

    if not from_id or not to_id:
        return jsonify({'error': 'Missing from_id or to_id'}), 400

    custom = FlowchartEdgeCustomization.query.filter_by(
        from_task_id=from_id, to_task_id=to_id
    ).first()

    if custom:
        db.session.delete(custom)
        db.session.commit()

    return jsonify({'success': True})


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)
