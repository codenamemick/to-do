# Defines the DB structure

from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()


class StatusCache:
    """Request-scoped cache for computed statuses and counts."""
    _instance = None

    def __init__(self):
        self.statuses = {}
        self.upstream_counts = {}
        self.downstream_counts = {}
        self.downstream_goals = {}
        self.completion_counts = {}

    @classmethod
    def get(cls):
        """Get or create the cache instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def clear(cls):
        """Clear the cache (call at start of each request)."""
        cls._instance = None


# Task table
class Task(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    estimated_duration = db.Column(db.Float)
    unknown_dependencies = db.Column(db.Boolean, default=False)
    recurring = db.Column(db.Boolean, default=False)
    scheduled_datetime = db.Column(db.DateTime)
    fixed_time = db.Column(db.Boolean, default=False)  # If True, task is blocked until scheduled_datetime
    deadline = db.Column(db.DateTime)
    created_datetime = db.Column(db.DateTime)
    deleted = db.Column(db.Boolean, default=False)  # Soft delete flag
    awaiting = db.Column(db.Boolean, default=False)  # Someone else to complete

    # Recurring schedule fields
    recurrence_frequency = db.Column(db.String(20))  # daily, weekly, monthly, yearly
    recurrence_interval = db.Column(db.Integer, default=1)  # every N days/weeks/months/years
    recurrence_days = db.Column(db.String(20))  # for weekly: "0,1,2,3,4" (Mon-Fri), comma-separated
    recurrence_month_type = db.Column(db.String(20))  # 'day_of_month' or 'day_of_week'
    recurrence_end_type = db.Column(db.String(20))  # 'never', 'after', 'on_date'
    recurrence_end_count = db.Column(db.Integer)  # number of occurrences if end_type='after'
    recurrence_end_date = db.Column(db.Date)  # end date if end_type='on_date'

    # Relationships - use 'select' (eager) instead of 'dynamic' for better performance
    dependencies = db.relationship(
        'TaskDependency',
        foreign_keys='TaskDependency.task_id',
        backref='task',
        lazy='select',
        cascade='all, delete-orphan'
    )
    dependents = db.relationship(
        'TaskDependency',
        foreign_keys='TaskDependency.depends_on_id',
        backref='depends_on_task',
        lazy='select',
        cascade='all, delete-orphan'
    )
    completion_records = db.relationship(
        'CompletionRecord',
        backref='task',
        lazy='select',
        cascade='all, delete-orphan'
    )

    @property
    def is_complete(self):
        """Check if task is complete, using cache."""
        cache = StatusCache.get()
        if self.id not in cache.completion_counts:
            cache.completion_counts[self.id] = len(self.completion_records) > 0
        return cache.completion_counts[self.id]

    @property
    def status(self):
        """Get status with caching."""
        cache = StatusCache.get()
        if self.id in cache.statuses:
            return cache.statuses[self.id]

        result = self._compute_status(set())
        cache.statuses[self.id] = result
        return result

    def _compute_status(self, seen):
        """Compute status based on task state, with cycle protection."""
        cache = StatusCache.get()

        # Check cache first
        if self.id in cache.statuses:
            return cache.statuses[self.id]

        # Cycle guard
        if self.id in seen:
            return 'Blocked'

        seen.add(self.id)

        # Check if completed (has any completion records)
        if self.is_complete:
            cache.statuses[self.id] = 'Complete'
            return 'Complete'

        # Check if fixed-time event
        if self.fixed_time:
            cache.statuses[self.id] = 'Event'
            return 'Event'

        # Check if this task is indefinitely recurring
        if self.recurring and self.recurrence_end_type not in ('after', 'on_date'):
            cache.statuses[self.id] = 'Recurring'
            return 'Recurring'

        # Check if it's a goal (unknown dependencies) - takes priority over blocked
        if self.unknown_dependencies:
            cache.statuses[self.id] = 'Goal'
            return 'Goal'

        # Check if any dependency is indefinitely recurring (makes this task a Goal)
        for dep in self.dependencies:
            dep_task = dep.depends_on_task
            if dep_task.deleted:
                continue
            if dep_task.recurring and dep_task.recurrence_end_type not in ('after', 'on_date'):
                cache.statuses[self.id] = 'Goal'
                return 'Goal'

        # Check if it's Awaiting
        if self.awaiting:
            cache.statuses[self.id] = 'Awaiting'
            return 'Awaiting'

        # Check if blocked - only blocked if a dependency is Awaiting
        # or has Awaiting in its dependency chain
        if self._has_awaiting_in_chain(set()):
            cache.statuses[self.id] = 'Blocked'
            return 'Blocked'

        # Check if has incomplete dependencies (but none are Awaiting) - still Free
        # Default status (scheduled tasks are also Free)
        cache.statuses[self.id] = 'Free'
        return 'Free'

    def _has_awaiting_in_chain(self, visited):
        """Check if any dependency (recursively) is Awaiting."""
        if self.id in visited:
            return False
        visited.add(self.id)

        for dep in self.dependencies:
            dep_task = dep.depends_on_task
            if dep_task.deleted:
                continue
            # Skip completed tasks
            if dep_task.is_complete:
                continue
            # Check if this dependency is Awaiting
            if dep_task.awaiting:
                return True
            # Recursively check the dependency's dependencies
            if dep_task._has_awaiting_in_chain(visited):
                return True

        return False


    @property
    def incomplete_dependencies(self):
        """Return list of incomplete, non-deleted dependency tasks."""
        return [
            dep.depends_on_task for dep in self.dependencies
            if not dep.depends_on_task.deleted and dep.depends_on_task.status != 'Complete'
        ]

    def get_downstream_count(self, visited=None):
        """Count all tasks that depend on this task (recursively), excluding Complete and deleted."""
        cache = StatusCache.get()

        # Only use cache for top-level calls (visited is None)
        if visited is None:
            if self.id in cache.downstream_counts:
                return cache.downstream_counts[self.id]
            visited = set()
            is_top_level = True
        else:
            is_top_level = False

        count = 0
        for dep in self.dependents:
            dependent_task = dep.task
            if dependent_task.id in visited or dependent_task.deleted:
                continue
            visited.add(dependent_task.id)
            # Count non-Complete tasks (including Goals)
            if dependent_task.status != 'Complete':
                count += 1
            # Recursively count downstream tasks
            count += dependent_task.get_downstream_count(visited)

        if is_top_level:
            cache.downstream_counts[self.id] = count
        return count

    def get_upstream_count(self, visited=None):
        """Count all tasks this task depends on (recursively), excluding Complete and deleted."""
        cache = StatusCache.get()

        # Only use cache for top-level calls (visited is None)
        if visited is None:
            if self.id in cache.upstream_counts:
                return cache.upstream_counts[self.id]
            visited = set()
            is_top_level = True
        else:
            is_top_level = False

        count = 0
        for dep in self.dependencies:
            dep_task = dep.depends_on_task
            if dep_task.id in visited or dep_task.deleted:
                continue
            visited.add(dep_task.id)
            # Count non-Complete tasks
            if dep_task.status != 'Complete':
                count += 1
            # Recursively count upstream tasks
            count += dep_task.get_upstream_count(visited)

        if is_top_level:
            cache.upstream_counts[self.id] = count
        return count

    @property
    def upstream_count(self):
        """Property wrapper for get_upstream_count() for use in Jinja2."""
        return self.get_upstream_count()

    @property
    def downstream_count(self):
        """Property wrapper for get_downstream_count() for use in Jinja2 sorting."""
        return self.get_downstream_count()

    def get_downstream_goals(self, visited=None):
        """Get all Goal tasks that depend on this task (directly or indirectly).

        Stops traversing at the first Goal in each chain - does not include
        goals that depend on other goals.
        """
        cache = StatusCache.get()

        # Only use cache for top-level calls (visited is None)
        if visited is None:
            if self.id in cache.downstream_goals:
                return cache.downstream_goals[self.id]
            visited = set()
            is_top_level = True
        else:
            is_top_level = False

        goals = []
        for dep in self.dependents:
            dependent_task = dep.task
            if dependent_task.id in visited:
                continue
            visited.add(dependent_task.id)

            # Skip deleted tasks
            if dependent_task.deleted:
                continue

            # Skip completed tasks
            if dependent_task.status == 'Complete':
                continue

            # If this dependent is a Goal, add it and stop this branch
            if dependent_task.status == 'Goal':
                goals.append(dependent_task)
                # Don't recurse further - stop at the first goal in the chain
            else:
                # Not a goal, so continue traversing
                goals.extend(dependent_task.get_downstream_goals(visited))

        if is_top_level:
            cache.downstream_goals[self.id] = goals
        return goals


# Task dependencies table
class TaskDependency(db.Model):
    """Junction table for task dependencies."""
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey('task.id'), nullable=False)
    depends_on_id = db.Column(db.Integer, db.ForeignKey('task.id'), nullable=False)

    __table_args__ = (
        db.UniqueConstraint('task_id', 'depends_on_id', name='unique_dependency'),
    )


# Completion records table
class CompletionRecord(db.Model):
    """Tracks when and how a task was completed."""
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey('task.id'), nullable=False)
    start_time = db.Column(db.DateTime, nullable=False)
    end_time = db.Column(db.DateTime, nullable=False)
    interval_number = db.Column(db.Integer, default=1)


# Flowchart customization tables
class FlowchartNodePosition(db.Model):
    """Custom node positions in flowchart (overrides auto-layout)."""
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey('task.id'), nullable=False, unique=True)
    x = db.Column(db.Float, nullable=False)
    y = db.Column(db.Float, nullable=False)


class FlowchartEdgeCustomization(db.Model):
    """Custom edge routing and port assignments."""
    id = db.Column(db.Integer, primary_key=True)
    from_task_id = db.Column(db.Integer, db.ForeignKey('task.id'), nullable=False)
    to_task_id = db.Column(db.Integer, db.ForeignKey('task.id'), nullable=False)
    source_port = db.Column(db.String(10), default='top')  # top/bottom/left/right
    target_port = db.Column(db.String(10), default='bottom')
    waypoints_json = db.Column(db.Text)  # JSON: [{"x": 100, "y": 200}, ...]

    __table_args__ = (
        db.UniqueConstraint('from_task_id', 'to_task_id', name='unique_edge_custom'),
    )
