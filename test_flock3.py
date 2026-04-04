import fcntl
import time
import sys
import os

fd = open("test2.lock", "w")
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
fd.write(str(os.getpid()))
fd.flush()
print("Locked. Exiting...")
sys.exit(0)
