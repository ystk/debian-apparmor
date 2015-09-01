#include <fcntl.h>
#include <string.h>
#include <sys/apparmor.h>

#define SD_ID_MAGIC     0x8c235e38

inline int do_open (char * file)
{
	int fd, rc;
	char buf[128];
	const char *data="hello world";

	fd=open(file, O_RDWR, 0);
	if (fd == -1){
		fprintf(stderr, "FAIL: open %s failed - %s\n",
			file,
			strerror(errno));
		return errno;
	}

        rc=write(fd, data, strlen(data));

        if (rc != strlen(data)){
                fprintf(stderr, "FAIL: write failed - %s\n",
                        strerror(errno));
                return errno;
        }

        (void)lseek(fd, 0, SEEK_SET);
        rc=read(fd, buf, sizeof(buf));

        if (rc != strlen(data)){
                fprintf(stderr, "FAIL: read failed - %s\n",
                        strerror(errno));
                return errno;
        }

        if (memcmp(buf, data, strlen(data)) != 0){
                fprintf(stderr, "FAIL: comparison failed - %s\n",
                        strerror(errno));
                return errno;
        }

	close(fd);

	return 0;
}
