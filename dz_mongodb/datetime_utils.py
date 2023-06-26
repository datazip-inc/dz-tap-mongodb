from datetime import datetime

def is_date_time(datetime_str: str) -> bool:
    """
    Args:
        datetime_str: a string 
    
    Returns: True if datetime_str is string representation of datetime , False otherwise
    """

    datetime_formats = [
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S %z",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f"
    ]

    for dt_fmt in datetime_formats:
        try:
            datetime.strptime(datetime_str, dt_fmt)
            return True
        except ValueError:
            pass 

    return False




