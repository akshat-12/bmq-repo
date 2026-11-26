GEM5 := /home/om/workspace/Modeling-Project/gem5
CFLAGS := -O2 -pthread -std=c11 -Wall -I$(GEM5)/include

all: cohmicro cohmicro-native

cohmicro: cohmicro.c
	$(CC) $(CFLAGS) -static -o $@ $< $(GEM5)/util/m5/build/x86/out/libm5.a

# host build for sanity checks (no m5ops)
cohmicro-native: cohmicro.c
	$(CC) $(CFLAGS) -DNATIVE_TEST -o $@ $<

clean:
	rm -f cohmicro cohmicro-native
