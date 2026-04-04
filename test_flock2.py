import fcntl
fd = open("test.lock", "w")
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print("Acquired")
except BlockingIOError:
    print("Blocked")
