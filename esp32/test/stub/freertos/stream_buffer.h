#pragma once
#include <stddef.h>
typedef struct sb *StreamBufferHandle_t;
StreamBufferHandle_t xStreamBufferCreate(size_t size, size_t trigger);
size_t xStreamBufferSend(StreamBufferHandle_t b, const void *d, size_t n, unsigned wait);
size_t xStreamBufferReceive(StreamBufferHandle_t b, void *d, size_t n, unsigned wait_ms);
void xStreamBufferReset(StreamBufferHandle_t b);
