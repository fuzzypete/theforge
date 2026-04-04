import fcntl
import time
import sys

fd = open("test.lock", "w")
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
fd.write("12345")
fd.flush()
print("Locked. Sleeping...")
time.sleep(10)
