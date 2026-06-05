#ifndef SCHEDCP_SLUG_SCHED_MARKER_H
#define SCHEDCP_SLUG_SCHED_MARKER_H

#ifdef __linux__
#include <errno.h>
#include <linux/bpf.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>

#ifndef SYS_bpf
#ifdef __NR_bpf
#define SYS_bpf __NR_bpf
#endif
#endif

#ifndef SLUG_HINT_MAP_DEFAULT
#define SLUG_HINT_MAP_DEFAULT "/sys/fs/bpf/schedcp/slug_task_hints"
#endif

#ifdef __cplusplus
#define SLUG_THREAD_LOCAL thread_local
#else
#define SLUG_THREAD_LOCAL __thread
#endif

enum slug_sched_hint {
	SLUG_SCHED_HINT_NONE = 0,
	SLUG_SCHED_HINT_READ = 1,
	SLUG_SCHED_HINT_WRITE = 2,
	SLUG_SCHED_HINT_BALANCED = 3,
	SLUG_SCHED_HINT_PIPELINE = 4,
};

static inline int slug_bpf_syscall(enum bpf_cmd cmd, union bpf_attr *attr)
{
#ifdef SYS_bpf
	return (int)syscall(SYS_bpf, cmd, attr, sizeof(*attr));
#else
	(void)cmd;
	(void)attr;
	errno = ENOSYS;
	return -1;
#endif
}

static inline int slug_bpf_obj_get(const char *path)
{
	union bpf_attr attr;

	memset(&attr, 0, sizeof(attr));
	attr.pathname = (uint64_t)(uintptr_t)path;
	return slug_bpf_syscall(BPF_OBJ_GET, &attr);
}

static inline int slug_bpf_map_update_elem(int fd, const void *key,
					   const void *value, uint64_t flags)
{
	union bpf_attr attr;

	memset(&attr, 0, sizeof(attr));
	attr.map_fd = fd;
	attr.key = (uint64_t)(uintptr_t)key;
	attr.value = (uint64_t)(uintptr_t)value;
	attr.flags = flags;
	return slug_bpf_syscall(BPF_MAP_UPDATE_ELEM, &attr);
}

static inline int slug_hint_map_fd(void)
{
	static SLUG_THREAD_LOCAL int fd = -2;

	if (fd == -2) {
		const char *path = getenv("SLUG_HINT_MAP");

		if (!path || !path[0])
			path = SLUG_HINT_MAP_DEFAULT;
		fd = slug_bpf_obj_get(path);
	}
	return fd;
}

static inline int slug_mark_bb(unsigned int hint)
{
	static SLUG_THREAD_LOCAL unsigned int cached_hint = UINT32_MAX;
	int fd;
	uint32_t tid, value;

	if (cached_hint == hint)
		return 0;

	fd = slug_hint_map_fd();
	if (fd < 0)
		return fd;

	tid = (uint32_t)syscall(SYS_gettid);
	value = (uint32_t)hint;
	if (slug_bpf_map_update_elem(fd, &tid, &value, BPF_ANY) == 0) {
		cached_hint = hint;
		return 0;
	}
	return -1;
}
#else
enum slug_sched_hint {
	SLUG_SCHED_HINT_NONE = 0,
	SLUG_SCHED_HINT_READ = 1,
	SLUG_SCHED_HINT_WRITE = 2,
	SLUG_SCHED_HINT_BALANCED = 3,
	SLUG_SCHED_HINT_PIPELINE = 4,
};

static inline int slug_mark_bb(unsigned int hint)
{
	(void)hint;
	return -1;
}
#endif

#define SLUG_MARK_NONE_BB() ((void)slug_mark_bb(SLUG_SCHED_HINT_NONE))
#define SLUG_MARK_READ_BB() ((void)slug_mark_bb(SLUG_SCHED_HINT_READ))
#define SLUG_MARK_WRITE_BB() ((void)slug_mark_bb(SLUG_SCHED_HINT_WRITE))
#define SLUG_MARK_BALANCED_BB() ((void)slug_mark_bb(SLUG_SCHED_HINT_BALANCED))
#define SLUG_MARK_PIPELINE_BB() ((void)slug_mark_bb(SLUG_SCHED_HINT_PIPELINE))

#endif /* SCHEDCP_SLUG_SCHED_MARKER_H */
