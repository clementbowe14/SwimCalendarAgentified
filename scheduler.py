import datetime
import math

def ceil_dt(dt, delta):
    """
    Rounds up a datetime object to the nearest multiple of delta.
    delta must be a datetime.timedelta object.
    """
    return dt + (datetime.datetime.min - dt) % delta

class SchedulerConstraintEngine:
    def __init__(self, instructor_address):
        self.instructor_address = instructor_address

    def get_travel_buffer(self, distance_miles):
        """Returns the buffer in minutes based on distance."""
        if distance_miles < 5.0:
            return 10
        elif 5.0 <= distance_miles <= 10.0:
            return 20
        else:
            return 30

    def compute_earliest_start_after_lesson(self, prev_end_time, travel_time_mins, distance_miles):
        buffer_mins = self.get_travel_buffer(distance_miles)
        earliest_start = prev_end_time + datetime.timedelta(minutes=travel_time_mins + buffer_mins)
        return self._apply_15min_ceiling(earliest_start)

    def compute_earliest_start_after_break(self, break_end_time, travel_time_mins):
        earliest_start = break_end_time + datetime.timedelta(minutes=travel_time_mins + 15)
        return self._apply_15min_ceiling(earliest_start)

    def _apply_15min_ceiling(self, dt):
        """
        Rounds the given datetime UP to the nearest 15-minute boundary.
        Using ceiling ensures that constraints are strictly met (no time is shaved off).
        """
        delta = datetime.timedelta(minutes=15)
        # Handle timezone awareness safely or assume naive/UTC
        # datetime.min is naive, so we should calculate offset differently if needed.
        # A simpler approach:
        discard = datetime.timedelta(minutes=dt.minute % 15,
                                     seconds=dt.second,
                                     microseconds=dt.microsecond)
        if discard == datetime.timedelta(0):
            return dt
        
        dt += datetime.timedelta(minutes=15) - discard
        return dt

# test rounding
s = SchedulerConstraintEngine("A")
print(s._apply_15min_ceiling(datetime.datetime(2023, 1, 1, 10, 7)))
print(s._apply_15min_ceiling(datetime.datetime(2023, 1, 1, 10, 0)))
print(s._apply_15min_ceiling(datetime.datetime(2023, 1, 1, 10, 15)))
print(s._apply_15min_ceiling(datetime.datetime(2023, 1, 1, 10, 16)))
