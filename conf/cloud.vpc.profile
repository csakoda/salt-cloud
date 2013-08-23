appature_vpc:
  provider: my-aws
  cidr-block: 10.1.0.0/16
  routetables:
    public_rtb:
      dest-cidr-block: 0.0.0.0/0, 
      gateway-id: new # TODO implement 'new' for IGW
      subnets:
        - 10.1.0.0/24 # TODO needs alternate syntax for az's
    private_rtb:
      dest-cidr-block: 0.0.0.0/0
      instance-id: new # TODO implement 'new' for NAT
      subnets:
        - 10.1.1.0/24
  securitygroups:
    group-name:
      group-desc: somedesc
      inbound_rules:
        - { protocol: tcp, from-port: 80, to-port: 80, ip-range: 0.0.0.0/0 }
        - { protocol: tcp, from-port: 443, to-port: 443, ip-range: 0.0.0.0/0 }
      outbound_rules:
        - { protocol: tcp, from-port: 80, to-port: 80, ip-range: 0.0.0.0/0 }
        - { protocol: tcp, from-port: 443, to-port: 443, ip-range: 0.0.0.0/0 }
  nat: vpc_nat_small