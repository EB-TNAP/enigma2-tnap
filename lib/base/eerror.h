#ifndef __E_ERROR__
#define __E_ERROR__

#include <string>
#include <map>
#include <new>
#include <libsig_comp.h>
#include <sys/time.h>

// to use memleak check change the following in configure.ac
// * add -DMEMLEAK_CHECK and -rdynamic to CPP_FLAGS

#ifdef MEMLEAK_CHECK
#define BACKTRACE_DEPTH 5
#include <map>
#include <lib/base/elock.h>
#include <execinfo.h>
#include <string>
#include <new>
#include <cxxabi.h>
typedef struct
{
	unsigned int address;
	unsigned int size;
	const char *file;
	void *backtrace[BACKTRACE_DEPTH];
	unsigned char btcount;
	unsigned short line;
	unsigned char type;
} ALLOC_INFO;

typedef std::map<unsigned int, ALLOC_INFO> AllocList;

extern AllocList *allocList;
extern pthread_mutex_t memLock;

static inline void AddTrack(unsigned int addr,  unsigned int asize,  const char *fname, unsigned int lnum, unsigned int type)
{
	ALLOC_INFO info;

	if(!allocList)
		allocList = new(AllocList);

	info.address = addr;
	info.file = fname;
	info.line = lnum;
	info.size = asize;
	info.type = type;
	info.btcount = 0; //backtrace( info.backtrace, BACKTRACE_DEPTH );
	singleLock s(memLock);
	(*allocList)[addr]=info;
};

static inline void RemoveTrack(unsigned int addr, unsigned int type)
{
	if(!allocList)
		return;
	AllocList::iterator i;
	singleLock s(memLock);
	i = allocList->find(addr);
	if ( i != allocList->end() )
	{
		if ( i->second.type != type )
			i->second.type=3;
		else
			allocList->erase(i);
	}
};

inline void * operator new(size_t size, const char *file, int line)
{
	void *ptr = (void *)malloc(size);
	AddTrack((unsigned int)ptr, size, file, line, 1);
	return(ptr);
};

inline void operator delete(void *p)
{
	RemoveTrack((unsigned int)p,1);
	free(p);
};

inline void * operator new[](size_t size, const char *file, int line)
{
	void *ptr = (void *)malloc(size);
	AddTrack((unsigned int)ptr, size, file, line, 2);
	return(ptr);
};

inline void operator delete[](void *p)
{
	RemoveTrack((unsigned int)p, 2);
	free(p);
};

void DumpUnfreed();
#define new new(__FILE__, __LINE__)

#endif // MEMLEAK_CHECK

#ifndef NULL
#define NULL 0
#endif

#ifdef ASSERT
#undef ASSERT
#endif

#ifndef SWIG

#define CHECKFORMAT __attribute__ ((__format__(__printf__, 2, 3)))

/*
 * Current loglevel
 * Maybe set by ENIGMA_DEBUG_LVL environment variable.
 * main() will check the environemnt to set the values.
 */
extern int debugLvl;

void CHECKFORMAT eDebugImpl(int flags, const char*, ...);
enum { lvlTrace=5, lvlDebug=4, lvlInfo=3, lvlWarning=2, lvlError=1, lvlFatal=0 };

#define DEFAULT_DEBUG_LVL  4

#ifndef DEBUG
# define MAX_DEBUG_LEVEL 0
#else
# ifndef MAX_DEBUG_LEVEL
#  define MAX_DEBUG_LEVEL 5
# endif
#endif

#define _DBGFLG_NONEWLINE  1
#define _DBGFLG_NOTIME     2
#define _DBGFLG_FATAL      4
/* TNAP: bits 4-6 carry the message level into eDebugImpl so the runtime
 * console gate can be applied INSIDE the impl while the in-memory ring
 * buffer captures everything. Does not collide with the flag bits above. */
#define _DBGFLG_LVLSHIFT   4
#define _DBGFLG_LVLMASK    (0x7 << _DBGFLG_LVLSHIFT)
#define _DBGFLG_LVL(lvl)   (((lvl) & 0x7) << _DBGFLG_LVLSHIFT)

/* TNAP: size of the in-memory debug ring buffer that bsod.cpp dumps into
 * crash logs. RAM only - never touches flash. 256KB is minutes of full
 * level-4 history instead of the old 16KB (~150 lines / a few seconds). */
#define RINGBUFFER_SIZE 262144

/* When lvl is above MAX_DEBUG_LEVEL, the compiler will optimize the whole debug
 * statement away. This enables compile-time check of parameters and code.
 *
 * TNAP change (crash-log usefulness): messages up to lvlDebug are ALWAYS
 * formatted and stored in the ring buffer regardless of the runtime debug
 * level - debugLvl now only gates the console write (fd 2), inside
 * eDebugImpl. This is what makes crash logs carry full context on boxes
 * running at the default level 3. lvlTrace stays runtime-gated here so
 * hot eTrace paths cost nothing unless trace logging is enabled. */
#define eDebugLow(lvl, flags, ...) \
	do { \
		if (((lvl) <= MAX_DEBUG_LEVEL) && ((lvl) < lvlTrace || (lvl) <= debugLvl)) \
			eDebugImpl((flags) | _DBGFLG_LVL(lvl), __VA_ARGS__); \
	} while (0)
#define eFatal(...)			eDebugLow(lvlFatal, _DBGFLG_FATAL, __VA_ARGS__)
#define eLog(lvl, ...)			eDebugLow(lvl,        0,                 ##__VA_ARGS__)
#define eLogNoNewLineStart(lvl, ...)	eDebugLow(lvl,        _DBGFLG_NONEWLINE, ##__VA_ARGS__)
#define eLogNoNewLine(lvl, ...)		eDebugLow(lvl,        _DBGFLG_NOTIME | _DBGFLG_NONEWLINE, ##__VA_ARGS__)
#define eWarning(...)			eDebugLow(lvlWarning, 0,                   __VA_ARGS__)
#define eDebug(...)			eDebugLow(lvlDebug,   0,                   __VA_ARGS__)
#define eDebugNoNewLineStart(...)	eDebugLow(lvlDebug,   _DBGFLG_NONEWLINE,   __VA_ARGS__)
#define eDebugNoNewLine(...)		eDebugLow(lvlDebug,   _DBGFLG_NOTIME | _DBGFLG_NONEWLINE, __VA_ARGS__)
#define eTrace(...)			eDebugLow(lvlTrace,        0,                 ##__VA_ARGS__)
#define eTraceNoNewLineStart(...)	eDebugLow(lvlTrace, _DBGFLG_NONEWLINE,                 ##__VA_ARGS__)
#define eTraceNoNewLine(...)		eDebugLow(lvlTrace, _DBGFLG_NOTIME | _DBGFLG_NONEWLINE, ##__VA_ARGS__)
#define ASSERT(x) { if (!(x)) eFatal("%s:%d ASSERTION %s FAILED!", __FILE__, __LINE__, #x); }

#endif // SWIG

void ePythonOutput(const char *, int lvl = lvlDebug);
int eGetEnigmaDebugLvl();

#endif // __E_ERROR__
