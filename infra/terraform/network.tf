#
# One public subnet, one instance, one elastic IP.
#
# There is no private subnet and no NAT gateway. A NAT gateway costs more per
# month than the instance it would serve, and buys nothing here: the box must
# reach Kite over the internet either way, and nothing inbound is open except
# HTTPS. Shell access is SSM, which works outbound through the same route
# (M50). The security surface is the security group and the instance role, not
# the topology.
#

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = var.name }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = var.name }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.this.id
  cidr_block              = var.subnet_cidr
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = false # the elastic IP is the address, not a lease

  tags = { Name = "${var.name}-public" }
}

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

#
# Inbound: 443, and 80 only so the ACME HTTP-01 challenge and the redirect to
# HTTPS work. Nothing else -- in particular no 22, because there is no SSH
# daemon to reach and SSM is the way in (M50). Postgres listens on localhost
# and is not in this group at all.
#
resource "aws_security_group" "app" {
  name        = "${var.name}-app"
  description = "HTTPS in, everything out. No SSH: shell access is SSM."
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${var.name}-app" }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "https" {
  security_group_id = aws_security_group.app.id
  description       = "The operator's browser, and Zerodha's postbacks."
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "http" {
  security_group_id = aws_security_group.app.id
  description       = "ACME HTTP-01 and the permanent redirect to HTTPS."
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.app.id
  description       = "Kite's REST and websocket endpoints, SSM, CloudWatch, S3."
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

#
# M12a. This address is the launch gate: it is registered with Zerodha against
# the API key, and the engine refuses to place a live order until configuration
# records it and it matches the address the system is calling from. Destroying
# and recreating it means registering again with Zerodha before the next live
# order, so it is kept apart from the instance and outlives it.
#
resource "aws_eip" "app" {
  domain = "vpc"
  tags   = { Name = "${var.name}-static-ip" }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_eip_association" "app" {
  allocation_id = aws_eip.app.id
  instance_id   = aws_instance.app.id
}
