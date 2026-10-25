# Synthetic NVML-shaped CPU library for the P13 client tests. Not NVIDIA code; no device access.
# Layout mirrors the pinned 595.99.02 NVML offsets the hook depends on:
#   setter 0x7ca70, RM control wrapper 0x9c780, dispatch call 0x9c7fa (3 bytes), return site 0x9c7fd.
# The "RM" is fixture_rm: it serves A618/A61A/A619/E61B from a tiny in-memory policy model.
.section .text
.global nvmlInit_v2
.type nvmlInit_v2,@function
nvmlInit_v2:
  xor %eax,%eax
  ret
.global nvmlShutdown
.type nvmlShutdown,@function
nvmlShutdown:
  xor %eax,%eax
  ret
.global nvmlDeviceGetHandleByPciBusId_v2
.type nvmlDeviceGetHandleByPciBusId_v2,@function
nvmlDeviceGetHandleByPciBusId_v2:
  movq $0x1234,(%rsi)
  xor %eax,%eax
  ret
.org 0x7c470-0x10000,0x90
.global nvmlDeviceGetPowerManagementLimitConstraints
.type nvmlDeviceGetPowerManagementLimitConstraints,@function
nvmlDeviceGetPowerManagementLimitConstraints:
  movl $1000,(%rsi)
  movl $125000,(%rdx)
  xor %eax,%eax
  ret
.org 0x7ca70-0x10000,0x90
.global nvmlDeviceSetPowerManagementLimit
.type nvmlDeviceSetPowerManagementLimit,@function
nvmlDeviceSetPowerManagementLimit:
  push %rbp
  mov %rsp,%rbp
  sub $16,%rsp
  movb $1,(%rsp)              # role 1
  movb $0xfe,1(%rsp)
  movw $0,2(%rsp)
  mov %esi,4(%rsp)            # requested mW
  movl $0,8(%rsp)
  mov $0x2080e61e,%ecx
  mov %rsp,%r8
  mov $12,%r9d
  call fixture_wrapper
  leave
  ret
.global nvmlDeviceSetPowerManagementLimit_v2
.type nvmlDeviceSetPowerManagementLimit_v2,@function
nvmlDeviceSetPowerManagementLimit_v2:
  ud2
.org 0x9c780-0x10000,0x90
fixture_wrapper:
  push %rbp
  mov %rsp,%rbp
  # after the hook: ecx=command, r8=buffer, r9d=size
  mov %ecx,%edi
  mov %r8,%rsi
  mov %r9d,%edx
  lea fixture_table(%rip),%rax
.org 0x9c7fa-0x10000,0x90
  call *32(%rax)
  leave
  ret
fixture_rm:
  # edi=command, rsi=buffer, edx=size -> eax=status
  push %rbx
  cmp $0x2080e61e,%edi
  je .Lplain_setter
  cmp $0x2080a618,%edi
  je .Linfo
  cmp $0x2080a61a,%edi
  je .Lget
  cmp $0x2080a619,%edi
  je .Lstatus
  cmp $0x2080e61b,%edi
  je .Lset
  mov $0x56,%eax             # NOT_SUPPORTED
  jmp .Ldone
.Lplain_setter:
  incl fixture_plain_setter_calls(%rip)
  xor %eax,%eax
  jmp .Ldone
.Linfo:
  cmp $0x21b8,%edx
  jne .Lsize
  mov fixture_info(%rip),%rbx
  call fixture_copy
  xor %eax,%eax
  jmp .Ldone
.Lget:
  cmp $0x18d4,%edx
  jne .Lsize
  mov fixture_control(%rip),%rbx
  call fixture_copy
  xor %eax,%eax
  jmp .Ldone
.Lstatus:
  cmp $0x2e740,%edx
  jne .Lsize
  mov fixture_status(%rip),%rbx
  call fixture_copy
  xor %eax,%eax
  jmp .Ldone
.Lset:
  cmp $0x18d4,%edx
  jne .Lsize
  incl fixture_set_calls(%rip)
  # value = buffer[0x970]; refuse outside [1, fixture_max] with 0x1f, else store into the control and status model
  mov 0x970(%rsi),%eax
  cmp fixture_max(%rip),%eax
  ja .Lrange
  test %eax,%eax
  je .Lrange
  mov fixture_control(%rip),%rbx
  mov %eax,0x970(%rbx)
  mov fixture_status(%rip),%rbx
  mov %eax,0x1259c+4(%rbx)
  mov %eax,0x1259c+28(%rbx)
  mov %eax,0x1259c+180(%rbx)
  mov %eax,0x1259c+40(%rbx)
  mov %eax,fixture_last_set(%rip)
  xor %eax,%eax
  jmp .Ldone
.Lrange:
  mov $0x1f,%eax
  jmp .Ldone
.Lsize:
  mov $0x29,%eax
.Ldone:
  pop %rbx
  ret
fixture_copy:
  # copy edx bytes from rbx to rsi
  push %rdi
  push %rsi
  mov %rsi,%rdi
  mov %rbx,%rsi
  mov %edx,%ecx
  rep movsb
  pop %rsi
  pop %rdi
  ret
.section .data
.p2align 3
.global fixture_info,fixture_control,fixture_status,fixture_max,fixture_set_calls,fixture_plain_setter_calls,fixture_last_set
.protected fixture_info,fixture_control,fixture_status,fixture_max,fixture_set_calls,fixture_plain_setter_calls,fixture_last_set
fixture_info: .quad 0
fixture_control: .quad 0
fixture_status: .quad 0
fixture_max: .long 99000
fixture_set_calls: .long 0
fixture_plain_setter_calls: .long 0
fixture_last_set: .long 0
fixture_table: .quad 0,0,0,0,fixture_rm
.section .note.GNU-stack,"",@progbits
