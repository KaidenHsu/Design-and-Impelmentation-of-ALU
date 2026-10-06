// Combinational homework multiplier for normalized binary32 operands.
// Round to nearest, with halfway magnitudes rounded upward. Exponents wrap;
// zero, subnormal, infinity, NaN and exponent range checks are outside this design.
module FLP_mul(
    input [31:0] a,
    input [31:0] b,
    output [31:0] d
);
    wire sd;
    wire [7:0] ea, eb;
    wire [23:0] ma, mb;

    wire [8:0] esum_biased;
    wire [8:0] esum;
    wire [8:0] esum_norm;
    wire [8:0] esum_renorm;

    wire [47:0] md_raw;
    wire [23:0] md_norm;
    wire [24:0] md_round;
    wire [22:0] md_renorm;

    // Unpack: the sign is separate from the unsigned Q1.23 significands.
    assign sd = a[31] ^ b[31];
    assign ea = a[30:23];
    assign eb = b[30:23];
    assign ma = {1'b1, a[22:0]};
    assign mb = {1'b1, b[22:0]};

    // Unsigned Q2.46 product and biased output exponent, modulo 512.
    assign md_raw = ma * mb;
    assign esum_biased = {1'b0, ea} + {1'b0, eb};
    assign esum = esum_biased - 9'd127;

    // Store 24 fractional bits; normalized Q1.24 has an implicit leading 1.
    // A product >= 2 requires a right shift and one exponent increment.
    assign md_norm = md_raw[47] ? md_raw[46:23] : md_raw[45:22];
    assign esum_norm = esum + {8'b0, md_raw[47]};

    // Restore the hidden 1, retain 23 fraction bits and add the guard bit.
    // The extra high bit captures rounding from 1.111... to 10.000... .
    assign md_round = {2'b01, md_norm[23:1]} + {24'b0, md_norm[0]};

    // Remove the hidden bit, with another right shift on rounding overflow.
    assign md_renorm = md_round[24] ? md_round[23:1] : md_round[22:0];
    assign esum_renorm = esum_norm + {8'b0, md_round[24]};

    // Pack the exponent modulo 256; no overflow/underflow saturation.
    assign d = {sd, esum_renorm[7:0], md_renorm};
endmodule

