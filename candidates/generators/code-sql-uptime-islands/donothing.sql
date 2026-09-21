SELECT service, checked_at AS started_at, checked_at AS ended_at, 0 AS duration_min, 0 AS failed_checks, 0 AS severity_rank
FROM checks WHERE 1 = 0;
