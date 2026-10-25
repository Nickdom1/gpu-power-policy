# Synthetic CPU ABI only; no NVIDIA implementation or device operations.
.section .text
.org 0x7c470-0x10000,0x90
.global nvmlDeviceGetPowerManagementLimitConstraints
.type nvmlDeviceGetPowerManagementLimitConstraints,@function
nvmlDeviceGetPowerManagementLimitConstraints:
  push %rbp
  mov %rsp,%rbp
  mov %rdi,fixture_memory(%rip)
  call fixture_read
  xor %eax,%eax
  leave
  ret
.org 0x7ca70-0x10000,0x90
.global nvmlDeviceSetPowerManagementLimit
.type nvmlDeviceSetPowerManagementLimit,@function
nvmlDeviceSetPowerManagementLimit:
  ud2
.global nvmlDeviceSetPowerManagementLimit_v2
.type nvmlDeviceSetPowerManagementLimit_v2,@function
nvmlDeviceSetPowerManagementLimit_v2:
  ud2
.org 0x9c780-0x10000,0x90
fixture_wrapper:
  mov %ecx,fixture_query(%rip)
  push %rbp
  mov %rsp,%rbp
  mov %r8,%rcx
  mov %r9d,%r8d
  mov fixture_memory(%rip),%rax
  cmpl $1,0x220c(%rax)
  jne 1f
  xor %r8d,%r8d
1:
  mov fixture_query(%rip),%edx
  lea fixture_table(%rip),%rax
.org 0x9c7fa-0x10000,0x90
  call *32(%rax)
  ud2 # captured before retry or original reply parser
.org 0xf3180-0x10000,0x90
fixture_read:
  push %rbp
  mov %rsp,%rbp
  mov fixture_memory(%rip),%r8
  mov 0x2200(%r8),%ecx
  mov 0x2204(%r8),%r9d
  cmpl $2,0x220c(%r8)
  je fixture_wrong_caller
  jmp fixture_call
.org 0xf320e-0x10000,0x90
fixture_call:
  call fixture_wrapper
  ud2
.org 0xf3220-0x10000,0x90
fixture_wrong_caller:
  call fixture_wrapper
  ud2
fixture_callback:
  mov %rcx,%rdi
  mov fixture_payload(%rip),%rsi
  mov %r8d,%ecx
  rep movsb
  mov fixture_memory(%rip),%rax
  cmpl $3,0x220c(%rax)
  jne 2f
  movb $0,0(%rdi) # deliberate guard corruption, fixture only
2:
  cmpl $4,0x220c(%rax)
  jne 3f
  movb $0xab,0(%rax) # deliberate original-buffer mutation, fixture only
3:
  mov 0x2208(%rax),%eax
  ret
.section .data
.p2align 3
fixture_memory: .quad 0
fixture_query: .long 0
.p2align 3
.global fixture_payload
.protected fixture_payload
fixture_payload: .quad 0
fixture_table: .quad 0,0,0,0,fixture_callback
.section .note.GNU-stack,"",@progbits
